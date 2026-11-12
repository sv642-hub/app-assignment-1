import asyncio
import json

from fastapi import FastAPI, HTTPException

app = FastAPI()


@app.post("/v1/chat/completions")
async def chat_completions(simulate: str = "ok") -> dict:
    if simulate == "unavailable":
        raise HTTPException(status_code=503, detail="Simulated backend outage")
    if simulate == "slow":
        await asyncio.sleep(2)
    reply = {
        "intent": "unknown",
        "reply": "Could you describe the banking problem you need help with?",
        "policy_facts": [],
    }

    content = json.dumps(reply)
    if simulate == "invalid_json":
        content = '{"intent": "unknown",'

    return {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ]
    }
