"""
Stock Market Agent (Strands A2A) with STRICT "no useless MCP calls"

Goals:
- NEVER connect to MCP or list tools unless the incoming user message is a real market-data request.
- Keep MCP connections short-lived (idle TTL) and protected by hard timeouts.
- Avoid handler-level deadlocks/timeouts.
- Provide useful /diag, /tools, /probe endpoints for debugging.
- Work locally AND in Bedrock AgentCore runtime (A2A contract: /ping, /.well-known/agent-card.json via Strands, POST / JSON-RPC).

Notes:
- We do NOT call ensure_tools_loaded() unconditionally for POST / anymore.
- We parse JSON-RPC payload and only enable tools when the user's text looks like a market request.
- We set a request-level timeout for the whole handler.
"""

import os
import re
import json
import logging
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional, Tuple, Dict, Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import httpx
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.multiagent.a2a import A2AServer
from strands.tools.mcp import MCPClient

# ---------------------------------------------------------------------
# Load .env (local only; in AgentCore, env vars should be injected)
# ---------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Reduce noisy logs if you want:
# logging.getLogger("httpx").setLevel(logging.WARNING)
# logging.getLogger("mcp.client.streamable_http").setLevel(logging.INFO)

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
HOST, PORT = "0.0.0.0", 9000
RUNTIME_URL = os.getenv("AGENTCORE_RUNTIME_URL", "http://127.0.0.1:9000/")

ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()
if not ALPHAVANTAGE_API_KEY:
    raise RuntimeError("Missing ALPHAVANTAGE_API_KEY. Put it in runtime env vars or your .env (local).")

ALPHAVANTAGE_MCP_URL = f"https://mcp.alphavantage.co/mcp?apikey={ALPHAVANTAGE_API_KEY}"


def redact_url(url: str) -> str:
    return url.replace(ALPHAVANTAGE_API_KEY, "***REDACTED***")


# ---------------------------------------------------------------------
# Tunables (timeouts + policy)
# ---------------------------------------------------------------------
MCP_CONNECT_TIMEOUT_S = float(os.getenv("MCP_CONNECT_TIMEOUT_S", "8"))
MCP_LIST_TOOLS_TIMEOUT_S = float(os.getenv("MCP_LIST_TOOLS_TIMEOUT_S", "8"))
MCP_CLOSE_TIMEOUT_S = float(os.getenv("MCP_CLOSE_TIMEOUT_S", "5"))

HTTP_HANDLER_TIMEOUT_S = float(os.getenv("HTTP_HANDLER_TIMEOUT_S", "12"))
PROBE_TIMEOUT_S = float(os.getenv("PROBE_TIMEOUT_S", "5"))

MCP_IDLE_TTL_SECONDS = int(os.getenv("MCP_IDLE_TTL_SECONDS", "45"))

CB_FAILURE_THRESHOLD = int(os.getenv("MCP_CB_FAILURE_THRESHOLD", "3"))
CB_COOLDOWN_SECONDS = int(os.getenv("MCP_CB_COOLDOWN_SECONDS", "60"))

MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "200000"))  # parsing guard
MAX_USER_TEXT_LEN = int(os.getenv("MAX_USER_TEXT_LEN", "8000"))

# Optional: cache quote results to avoid repeated calls (same symbol)
QUOTE_CACHE_TTL_S = int(os.getenv("QUOTE_CACHE_TTL_S", "30"))


# ---------------------------------------------------------------------
# MCP state (per runtime instance)
# ---------------------------------------------------------------------
_mcp_client: Optional[MCPClient] = None
_mcp_error: Optional[str] = None

_connect_lock = asyncio.Lock()  # single-flight connect/close
_tools_lock = asyncio.Lock()    # single-flight list_tools

_tools_loaded: bool = False
_last_used_monotonic: float = 0.0
_idle_close_task: Optional[asyncio.Task] = None

_cb_failures: int = 0
_cb_open_until_monotonic: float = 0.0

_quote_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_cache_lock = asyncio.Lock()


def _build_mcp_client() -> MCPClient:
    # NOTE: streamable_http_client internally uses a GET stream for events.
    # We accept that; we just ensure it never blocks request handling indefinitely.
    return MCPClient(lambda: streamablehttp_client(url=ALPHAVANTAGE_MCP_URL))


def _cb_is_open() -> bool:
    return time.monotonic() < _cb_open_until_monotonic


def _cb_on_success():
    global _cb_failures, _cb_open_until_monotonic
    _cb_failures = 0
    _cb_open_until_monotonic = 0.0


def _cb_on_failure(err: Exception | str):
    """
    Count failures only for operations that block the user (connect/list_tools/tool calls).
    Do NOT count "stream reconnect noise" as failure.
    """
    global _cb_failures, _cb_open_until_monotonic, _mcp_error
    _cb_failures += 1
    _mcp_error = f"MCP error: {err}"

    if _cb_failures >= CB_FAILURE_THRESHOLD:
        _cb_open_until_monotonic = time.monotonic() + CB_COOLDOWN_SECONDS
        logger.warning(
            "🚫 MCP circuit breaker OPEN (failures=%d). Cooling down for %ds. Last error: %s",
            _cb_failures, CB_COOLDOWN_SECONDS, _mcp_error
        )


def _touch_last_used():
    global _last_used_monotonic
    _last_used_monotonic = time.monotonic()


async def _schedule_idle_close():
    global _idle_close_task

    if _idle_close_task and not _idle_close_task.done():
        return

    async def _runner():
        while True:
            await asyncio.sleep(1)
            if _mcp_client is None:
                return
            idle_for = time.monotonic() - _last_used_monotonic
            if idle_for >= MCP_IDLE_TTL_SECONDS:
                logger.info("🧹 MCP idle for %.1fs >= %ds. Closing connection.", idle_for, MCP_IDLE_TTL_SECONDS)
                await close_mcp()
                return

    _idle_close_task = asyncio.create_task(_runner())


async def ensure_mcp_connected() -> Optional[MCPClient]:
    """
    Connect once, with a hard timeout.
    """
    global _mcp_client, _mcp_error

    if _cb_is_open():
        return None

    if _mcp_client is not None:
        _touch_last_used()
        await _schedule_idle_close()
        return _mcp_client

    async with _connect_lock:
        if _cb_is_open():
            return None
        if _mcp_client is not None:
            _touch_last_used()
            await _schedule_idle_close()
            return _mcp_client

        _mcp_error = None
        client = _build_mcp_client()

        try:
            await asyncio.wait_for(asyncio.to_thread(client.__enter__), timeout=MCP_CONNECT_TIMEOUT_S)
            _mcp_client = client
            _cb_on_success()
            _touch_last_used()
            await _schedule_idle_close()
            logger.info("✅ Alpha Vantage MCP connected")
            return _mcp_client

        except asyncio.TimeoutError:
            _mcp_client = None
            _cb_on_failure(f"connect timeout after {MCP_CONNECT_TIMEOUT_S}s")
            logger.error("❌ MCP connect timed out after %ss", MCP_CONNECT_TIMEOUT_S)
            return None

        except Exception as e:
            _mcp_client = None
            _cb_on_failure(e)
            logger.exception("❌ MCP connect failed: %s", e)
            return None


async def close_mcp():
    global _mcp_client, _tools_loaded
    if _mcp_client is None:
        return

    async with _connect_lock:
        if _mcp_client is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_mcp_client.__exit__, None, None, None),
                timeout=MCP_CLOSE_TIMEOUT_S
            )
            logger.info("✅ MCP connection closed")
        except Exception as e:
            logger.warning("⚠️ MCP close error: %s", e)
        finally:
            _mcp_client = None
            _tools_loaded = False


async def ensure_tools_loaded(agent: Agent) -> bool:
    """
    Load MCP tools into agent ONCE, with a hard timeout.
    """
    global _tools_loaded

    if _tools_loaded and agent.tools:
        _touch_last_used()
        await _schedule_idle_close()
        return True

    async with _tools_lock:
        if _tools_loaded and agent.tools:
            _touch_last_used()
            await _schedule_idle_close()
            return True

        mcp_client = await ensure_mcp_connected()
        if not mcp_client:
            return False

        try:
            tools = await asyncio.wait_for(
                asyncio.to_thread(mcp_client.list_tools_sync),
                timeout=MCP_LIST_TOOLS_TIMEOUT_S
            )
            agent.tools = tools or []
            _tools_loaded = True
            _cb_on_success()
            _touch_last_used()
            await _schedule_idle_close()
            logger.info("✅ Loaded %d MCP tools into agent", len(agent.tools))
            return True

        except asyncio.TimeoutError:
            _cb_on_failure(f"list_tools timeout after {MCP_LIST_TOOLS_TIMEOUT_S}s")
            logger.error("❌ MCP list_tools timed out after %ss", MCP_LIST_TOOLS_TIMEOUT_S)
            await close_mcp()
            return False

        except Exception as e:
            _cb_on_failure(e)
            logger.exception("❌ Failed to load tools: %s", e)
            await close_mcp()
            return False


# ---------------------------------------------------------------------
# "No useless calls" intent gate
# ---------------------------------------------------------------------
_SYMBOL_RE = re.compile(r"\b[A-Z]{1,5}\b")


def _extract_user_text_from_a2a(payload: dict) -> str:
    """
    Extracts user text from Strands A2A JSON-RPC message/send payload.
    """
    try:
        if payload.get("method") != "message/send":
            return ""
        msg = ((payload.get("params") or {}).get("message") or {})
        parts = msg.get("parts") or []
        texts = []
        for p in parts:
            if isinstance(p, dict) and p.get("kind") == "text" and p.get("text"):
                texts.append(str(p["text"]))
        return "\n".join(texts).strip()
    except Exception:
        return ""


def _looks_like_market_request(text: str) -> bool:
    """
    Conservative heuristic:
    - If we think it might require market data, return True.
    - Otherwise False: do not touch MCP.
    """
    t = (text or "").strip()
    if not t:
        return False

    tl = t.lower()

    # obvious intents
    keywords = [
        "stock", "share", "price", "quote", "market", "trading",
        "ticker", "symbol", "last price", "current price",
        "open", "close", "volume", "high", "low",
    ]
    if any(k in tl for k in keywords):
        return True

    # pattern: "price of MSFT"
    if "price of" in tl or "quote for" in tl:
        return True

    # mentions common tickers
    if any(sym in tl for sym in ["msft", "aapl", "amzn", "goog", "googl", "tsla", "nvda"]):
        return True

    # if user included a likely ticker and asked a question mark about it
    if "?" in t and _SYMBOL_RE.search(t):
        # ex: "MSFT?" "AAPL now?"
        return True

    return False


async def _quote_cache_get(symbol: str) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    async with _cache_lock:
        item = _quote_cache.get(symbol.upper())
        if not item:
            return None
        exp, payload = item
        if now >= exp:
            _quote_cache.pop(symbol.upper(), None)
            return None
        return payload


async def _quote_cache_set(symbol: str, payload: Dict[str, Any]):
    exp = time.monotonic() + QUOTE_CACHE_TTL_S
    async with _cache_lock:
        _quote_cache[symbol.upper()] = (exp, payload)


# ---------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------
system_prompt = """You are a Stock Market Assistant powered by the Alpha Vantage MCP.

HARD RULES (non-negotiable):
- DO NOT call MCP tools unless the user is clearly requesting market data (quotes, fundamentals, time series).
- If the user is chatting or asking non-market questions, answer without MCP.
- NEVER output internal tool markup like <mcp_thinking> or <mcp_function_calls>.
- Never guess prices. If MCP is unavailable, say so briefly.
- Keep answers short and factual.
"""

agent = Agent(
    name="Stock Market Agent",
    description="Answers stock questions using Alpha Vantage MCP.",
    system_prompt=system_prompt,
    tools=[],  # injected only when needed
)

# ---------------------------------------------------------------------
# A2A Server -> FastAPI app
# ---------------------------------------------------------------------
a2a_server = A2AServer(agent=agent, http_url=RUNTIME_URL, serve_at_root=True)
app = a2a_server.to_fastapi_app()


# ---------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Startup: AlphaVantage MCP URL: %s", redact_url(ALPHAVANTAGE_MCP_URL))
    logger.info("Startup: RUNTIME_URL=%s", RUNTIME_URL)
    yield
    await close_mcp()


app.router.lifespan_context = lifespan


# ---------------------------------------------------------------------
# Request logging + strict handler timeout + "no useless calls" gate
# ---------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    path = request.url.path
    method = request.method
    logger.info("➡️ %s %s", method, path)

    async def _run():
        # Gate MCP/tool loading ONLY for JSON-RPC POST /
        if method == "POST" and path == "/":
            # Read and preserve body for downstream (FastAPI consumes it once)
            body = await request.body()
            if len(body) > MAX_BODY_BYTES:
                return JSONResponse(status_code=413, content={"error": "payload_too_large"})

            # Re-inject body so Strands can read it
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = receive  # type: ignore[attr-defined]

            # Decide if we touch MCP at all
            user_text = ""
            try:
                payload = json.loads(body[:MAX_USER_TEXT_LEN].decode("utf-8", errors="ignore"))
                user_text = _extract_user_text_from_a2a(payload)
            except Exception:
                user_text = ""

            if _looks_like_market_request(user_text):
                ok = await ensure_tools_loaded(agent)
                if not ok:
                    logger.warning("⚠️ MCP unavailable. Returning 503 (no guessing).")
                    return JSONResponse(
                        status_code=503,
                        content={
                            "error": "mcp_unavailable",
                            "detail": _mcp_error,
                            "circuit_breaker_open": _cb_is_open(),
                            "failures": _cb_failures,
                            "cooldown_remaining_s": max(0, int(_cb_open_until_monotonic - time.monotonic())),
                        },
                    )
            else:
                # Ensure we do NOT carry tools from previous requests if you want
                # absolute strictness. Comment this out if you prefer keeping tools loaded.
                agent.tools = []

        return await call_next(request)

    try:
        resp = await asyncio.wait_for(_run(), timeout=HTTP_HANDLER_TIMEOUT_S)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info("⬅️ %s %s -> %s (%.1fms)", method, path, resp.status_code, elapsed_ms)
        return resp
    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.error("⏱️ %s %s timed out after %.1fms", method, path, elapsed_ms)
        return JSONResponse(status_code=504, content={"error": "handler_timeout"})
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.exception("💥 %s %s raised after %.1fms: %s", method, path, elapsed_ms, e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


# ---------------------------------------------------------------------
# Required endpoints for AgentCore (A2A)
# ---------------------------------------------------------------------
@app.get("/ping")
def ping():
    logger.info("🏓 ping")
    return {"status": "Healthy"}


# ---------------------------------------------------------------------
# Debug endpoints (never auto-call MCP)
# ---------------------------------------------------------------------
@app.get("/probe")
async def probe():
    # Quick network check (does not use MCP client)
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S, follow_redirects=True) as c:
            r = await c.get(ALPHAVANTAGE_MCP_URL)
            return {"ok": True, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/diag")
async def diag():
    return {
        "runtime_url": RUNTIME_URL,
        "mcp_url": redact_url(ALPHAVANTAGE_MCP_URL),
        "mcp_connected": _mcp_client is not None,
        "tools_loaded": _tools_loaded,
        "tool_count": len(agent.tools or []),
        "circuit_breaker_open": _cb_is_open(),
        "failures": _cb_failures,
        "cooldown_remaining_s": max(0, int(_cb_open_until_monotonic - time.monotonic())),
        "error": _mcp_error,
        "timeouts": {
            "connect_s": MCP_CONNECT_TIMEOUT_S,
            "list_tools_s": MCP_LIST_TOOLS_TIMEOUT_S,
            "handler_s": HTTP_HANDLER_TIMEOUT_S,
            "probe_s": PROBE_TIMEOUT_S,
            "idle_ttl_s": MCP_IDLE_TTL_SECONDS,
            "quote_cache_ttl_s": QUOTE_CACHE_TTL_S,
        },
    }


@app.get("/tools")
async def tools():
    """
    Explicit manual call to load tools for debugging.
    This endpoint DOES use MCP (by design).
    """
    try:
        ok = await asyncio.wait_for(ensure_tools_loaded(agent), timeout=HTTP_HANDLER_TIMEOUT_S)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=504, content={"error": "timeout_loading_tools"})

    if not ok:
        return JSONResponse(
            status_code=503,
            content={
                "mcp_initialized": False,
                "circuit_breaker_open": _cb_is_open(),
                "failures": _cb_failures,
                "cooldown_remaining_s": max(0, int(_cb_open_until_monotonic - time.monotonic())),
                "error": _mcp_error,
                "mcp_url": redact_url(ALPHAVANTAGE_MCP_URL),
            },
        )

    names = [getattr(t, "tool_name", None) or getattr(t, "name", None) or str(t) for t in (agent.tools or [])]
    return {"mcp_initialized": True, "tool_count": len(names), "tools": names}


@app.post("/_debug_internal")
async def _debug_internal(body: dict):
    logger.info("🔥 INTERNAL DEBUG HIT %s", body)
    return {"ok": True}


# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        access_log=True,
        log_level="info",
    )
