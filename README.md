# 📈 Stock Market Agent — AWS Bedrock AgentCore Runtime Example

This repository demonstrates how to build, run, and deploy a **production-grade agent** using **AWS Bedrock AgentCore Runtime**, using with the **same codebase** running locally and in the cloud.

The agent answers stock market questions using **Alpha Vantage**, accessed via **MCP (Model Context Protocol)**, and is designed to showcase how fast you can go from **local development → managed, scalable runtime** with AgentCore.

---

## ⬇️ Download the Project

- **ZIP**: Click **Code → Download ZIP** on GitHub  
- **Git clone**:
```bash
git clone https://github.com/lsampaiorocha/agent-a2a-stock-advisor.git
cd agent-a2a-stock-advisor
```

---

## 🎯 What This Project Demonstrates

This project focuses on **AgentCore Runtime**, not finance itself.

It shows how you can:
- Develop an agent locally
- Integrate external tools via MCP
- Deploy quickly to a managed AWS runtime
- Reuse the same HTTP/A2A contract in production
- Get observability, scaling, and sessions out of the box

---

## 📊 Alpha Vantage (Market Data)

**Alpha Vantage** provides stock market data such as:
- Real-time quotes
- Open / close / high / low
- Volume
- Historical series

Website: https://www.alphavantage.co  
You need a **free API key**.

### Alpha Vantage MCP Endpoint
This agent uses Alpha Vantage via **MCP**, not direct REST calls:

```
https://mcp.alphavantage.co/mcp?apikey=YOUR_API_KEY
```

This exposes market data as **agent tools**, which mirrors how real production agents integrate external systems.

---

## ⚙️ Local Setup (using `uv`)

### 1️⃣ Install `uv`
```bash
pip install uv
```

---

### 1.1 Create and activate venv

**Windows (PowerShell)**:
```powershell
uv venv
.venv\Scripts\Activate.ps1
```

**macOS / Linux**:
```bash
uv venv
source .venv/bin/activate
```

---

### 1.2 Install dependencies
```bash
uv pip install -r requirements.txt
```

---

### 1.3 Set environment variables

Create a `.env` file in the project root:

```env
ALPHAVANTAGE_API_KEY=YOUR_API_KEY
```

---

## ▶️ Run Locally

```bash
python src/agent.py
```

The agent will start an HTTP server on port **9000**.

---

## 🧪 Test the Agent (HTTP / A2A)

```bash
curl -X POST http://localhost:9000/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "messageId": "test-1",
        "parts": [
          { "kind": "text", "text": "quote AAPL" }
        ]
      }
    }
  }'
```

---

## ☁️ Deploy to AWS AgentCore Runtime

Make sure AWS credentials are configured, then:

```bash
agentcore launch -a agent_mcp_stocks --auto-update-on-conflict --env ALPHAVANTAGE_API_KEY=YOUR_API_KEY
```

---

## 🧠 Notes on Design

- The agent dynamically loads MCP tools **only when needed**
- Works identically locally and in AgentCore Runtime
- No reliance on SSM or Parameter Store
- Environment variables are sufficient
- Designed for clarity, debuggability, and production parity

---

## 📎 Useful Endpoints

| Endpoint | Purpose |
|-------|--------|
| `/ping` | Health check |
| `/diag` | Runtime diagnostics |
| `/tools` | Manually load MCP tools |
| `/probe` | Network check to MCP |

---

## 🧩 Why This Matters

This repo shows how **AgentCore Runtime dramatically reduces the gap** between:
> “I have an agent running locally”  
and  
> “I have a scalable, managed, observable agent in production”

That is the real value demonstrated here.
