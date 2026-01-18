import os
import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn

from dotenv import load_dotenv

from mcp.client.streamable_http import streamablehttp_client

from strands import Agent
from strands.multiagent.a2a import A2AServer
from strands.tools.mcp import MCPClient

# ---------------------------------------------------------------------
# Load .env BEFORE reading environment variables
# ---------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Optional: extra visibility into MCP + HTTP traffic
# logging.getLogger("strands.tools.mcp").setLevel(logging.DEBUG)
# logging.getLogger("httpx").setLevel(logging.INFO)

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
HOST, PORT = "0.0.0.0", 9000
RUNTIME_URL = os.environ.get("AGENTCORE_RUNTIME_URL", "http://127.0.0.1:9000/")

ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY")
if not ALPHAVANTAGE_API_KEY:
    raise RuntimeError("Missing ALPHAVANTAGE_API_KEY. Put it in your .env or environment.")

ALPHAVANTAGE_MCP_URL = f"https://mcp.alphavantage.co/mcp?apikey={ALPHAVANTAGE_API_KEY}"

def redact_url(url: str) -> str:
    # Redact api key in logs and debug endpoints
    return url.replace(ALPHAVANTAGE_API_KEY, "***REDACTED***")

# ---------------------------------------------------------------------
# MCP client (global, lifecycle-managed)
# ---------------------------------------------------------------------
_mcp_client: MCPClient | None = None
_mcp_error: str | None = None


def _build_mcp_client() -> MCPClient:
    # Streamable HTTP transport (correct for /mcp endpoints)
    return MCPClient(lambda: streamablehttp_client(url=ALPHAVANTAGE_MCP_URL))


async def ensure_mcp_connected() -> MCPClient | None:
    """
    Lazily create + connect MCP client, then return it.
    Uses MCPClient's context manager (__enter__/__exit__) so we can keep
    the session open for the life of the FastAPI app.
    """
    global _mcp_client, _mcp_error

    if _mcp_client is not None:
        return _mcp_client

    _mcp_error = None
    try:
        _mcp_client = _build_mcp_client()
        # Enter context manager (sync) in a thread
        await asyncio.wait_for(asyncio.to_thread(_mcp_client.__enter__), timeout=15.0)
        logger.info("✅ Alpha Vantage MCP connected (streamable HTTP)")
        return _mcp_client

    except Exception as e:
        _mcp_error = f"MCP init failed: {e}"
        logger.exception(_mcp_error)
        _mcp_client = None
        return None


async def close_mcp():
    global _mcp_client
    if _mcp_client is None:
        return
    try:
        await asyncio.to_thread(_mcp_client.__exit__, None, None, None)
        logger.info("✅ MCP connection closed")
    finally:
        _mcp_client = None


# ---------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------
system_prompt = """You are a Stock Market Assistant powered by the Alpha Vantage MCP.

RULES:
- Use MCP tools for ALL stock market data (quotes, fundamentals, time series).
- Never guess prices or limits.
- Keep answers short and factual.
- If MCP is unavailable, say so explicitly.
"""

agent = Agent(
    name="Stock Market Agent",
    description="Answers stock questions using Alpha Vantage MCP.",
    system_prompt=system_prompt,
    tools=[],  # will be populated at startup
)


async def load_tools_into_agent():
    """
    Fetch tools from MCP and attach to the agent.
    """
    mcp_client = await ensure_mcp_connected()
    if not mcp_client:
        logger.error("❌ Cannot load tools: %s", _mcp_error)
        return

    try:
        tools = await asyncio.wait_for(asyncio.to_thread(mcp_client.list_tools_sync), timeout=10.0)
        agent.tools = tools or []
        logger.info("✅ Loaded %d MCP tools into agent", len(agent.tools))
    except Exception as e:
        logger.exception("❌ Tool load failed: %s", e)


# ---------------------------------------------------------------------
# A2A Server
# ---------------------------------------------------------------------
a2a_server = A2AServer(
    agent=agent,
    http_url=RUNTIME_URL,
    serve_at_root=True,
)

# ---------------------------------------------------------------------
# FastAPI lifespan (startup/shutdown)
# ---------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ✅ do NOT log the real key
    logger.info("Startup: AlphaVantage MCP URL: %s", redact_url(ALPHAVANTAGE_MCP_URL))
    await load_tools_into_agent()
    yield
    await close_mcp()


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------
# Debug endpoints
# ---------------------------------------------------------------------
@app.get("/ping")
def ping():
    return {"status": "healthy"}


@app.get("/tools")
async def tools():
    """
    Shows whether MCP is connected and which tools were discovered.
    """
    mcp_client = await ensure_mcp_connected()
    safe_url = redact_url(ALPHAVANTAGE_MCP_URL)

    if not mcp_client:
        return {"mcp_initialized": False, "error": _mcp_error, "mcp_url": safe_url}

    try:
        tools = await asyncio.to_thread(mcp_client.list_tools_sync)
        tool_names = [getattr(t, "tool_name", None) or getattr(t, "name", None) or str(t) for t in tools]
        return {
            "mcp_initialized": True,
            "mcp_url": safe_url,  # ✅ redacted
            "tool_count": len(tool_names),
            "tools": tool_names,
        }
    except Exception as e:
        return {"mcp_initialized": False, "error": str(e), "mcp_url": safe_url}


# Mount A2A JSON-RPC at /
app.mount("/", a2a_server.to_fastapi_app())

# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
