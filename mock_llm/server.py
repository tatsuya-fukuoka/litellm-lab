"""OpenAI互換のモックLLM。障害・遅延を実行時に注入できる（実キー不要）。

エンドポイント:
  POST /v1/chat/completions  ... 固定usage(100/100トークン)で応答
  POST /admin/mode           ... {"mode": "ok"|"error"|"slow", "status": 500, "delay_ms": 0}
  GET  /admin/stats          ... 受信リクエスト数
  POST /admin/reset          ... 統計とモードを初期化
  GET  /health
"""
import asyncio
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

NAME = os.environ.get("MOCK_NAME", "mock")
BASE_DELAY_MS = float(os.environ.get("MOCK_BASE_DELAY_MS", "50"))  # 通常時の疑似推論時間
PROMPT_TOKENS = int(os.environ.get("MOCK_PROMPT_TOKENS", "100"))
COMPLETION_TOKENS = int(os.environ.get("MOCK_COMPLETION_TOKENS", "100"))

app = FastAPI()
state = {"mode": "ok", "status": 500, "delay_ms": 0.0, "count": 0}


@app.get("/health")
async def health():
    return {"ok": True, "name": NAME}


@app.post("/admin/mode")
async def set_mode(req: Request):
    body = await req.json()
    state["mode"] = body.get("mode", "ok")
    state["status"] = int(body.get("status", 500))
    state["delay_ms"] = float(body.get("delay_ms", 0))
    return state


@app.get("/admin/stats")
async def stats():
    return state


@app.post("/admin/reset")
async def reset():
    state.update(mode="ok", status=500, delay_ms=0.0, count=0)
    return state


@app.post("/v1/chat/completions")
async def chat(req: Request):
    state["count"] += 1
    body = await req.json()
    mode = state["mode"]
    if mode == "error":
        return JSONResponse(
            status_code=state["status"],
            content={"error": {"message": f"{NAME}: injected failure", "type": "server_error"}},
        )
    delay = BASE_DELAY_MS + (state["delay_ms"] if mode == "slow" else 0.0)
    await asyncio.sleep(delay / 1000)
    return {
        "id": f"chatcmpl-{NAME}-{state['count']}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", NAME),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": f"reply from {NAME}"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": PROMPT_TOKENS,
            "completion_tokens": COMPLETION_TOKENS,
            "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
        },
    }
