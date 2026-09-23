import asyncio
import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.models.scenario import ChatRequest, ChatResponse, ScenarioConfiguration
from app.agents.scenario_agent import compile_scenario_agent

router = APIRouter(prefix="/agent", tags=["Agent"])

class ChatStreamRequest(BaseModel):
    """The AI SDK UIMessage request shape sent by useChat or standard chat clients."""

    messages: list[dict[str, Any]] = Field(default_factory=list)
    prompt: str | None = None
    message: str | dict[str, Any] | None = None
    id: str | None = None
    chatId: str | None = None


def _message_text(message: dict[str, Any]) -> str:
    # 1. Check parts (UIMessage format)
    parts = message.get("parts", [])
    if isinstance(parts, list):
        parts_text = "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
        if parts_text.strip():
            return parts_text.strip()

    # 2. Check content (string or list of dicts)
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    elif isinstance(content, list):
        list_text = "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        )
        if list_text.strip():
            return list_text.strip()

    # 3. Check direct text field
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    # 4. Check prompt, value, or message field inside message
    for key in ("value", "prompt", "message"):
        val = message.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    return ""


def _latest_scenario(messages: list[dict[str, Any]]) -> ScenarioConfiguration | None:
    for message in reversed(messages):
        for part in reversed(message.get("parts", [])):
            if isinstance(part, dict) and part.get("type") == "data-scenario" and isinstance(part.get("data"), dict):
                try:
                    return ScenarioConfiguration.model_validate(part["data"])
                except Exception:
                    pass
        for key in ("scenario", "data"):
            data = message.get(key)
            if isinstance(data, dict):
                try:
                    return ScenarioConfiguration.model_validate(data)
                except Exception:
                    pass
    return None


def _sse(chunk: dict[str, Any]) -> str:
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


@router.post("/chat")
async def chat_with_agent(req: ChatStreamRequest, request: Request):
    """Stream typed UIMessage chunks that AI SDK's useChat can consume."""
    messages = list(req.messages)

    if not messages:
        if req.prompt:
            messages = [{"role": "user", "content": req.prompt}]
        elif req.message:
            if isinstance(req.message, str):
                messages = [{"role": "user", "content": req.message}]
            elif isinstance(req.message, dict):
                messages = [req.message]

    user_messages = [message for message in messages if message.get("role") == "user"]
    if not user_messages:
        if messages:
            user_messages = [messages[-1]]
        else:
            raise HTTPException(status_code=400, detail="A user message is required.")

    prompt = _message_text(user_messages[-1]).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="The latest user message has no text.")

    history = [
        {"role": message.get("role"), "content": _message_text(message)}
        for message in messages[:-1]
        if _message_text(message)
    ]
    current_scenario = _latest_scenario(messages[:-1])

    async def stream():
        try:
            response = await compile_scenario_agent(
                prompt=prompt,
                current_scenario=current_scenario,
                history=history,
            )
            text_id = "scenario-response"
            yield _sse({"type": "start"})
            yield _sse({"type": "text-start", "id": text_id})
            for delta in re.findall(r"\S+\s*", response.message):
                if await request.is_disconnected():
                    return
                yield _sse({"type": "text-delta", "id": text_id, "delta": delta})
                await asyncio.sleep(0.015)
            yield _sse({"type": "text-end", "id": text_id})
            yield _sse({
                "type": "data-scenario",
                "id": "scenario",
                "data": response.scenario.model_dump(by_alias=True),
            })
            yield _sse({"type": "finish", "finishReason": "stop"})
        except Exception as error:
            yield _sse({"type": "error", "errorText": str(error)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-vercel-ai-ui-message-stream": "v1",
        },
    )


@router.post("/chat/complete", response_model=ChatResponse)
async def complete_chat(req: ChatRequest):
    """Non-streaming compatibility endpoint for direct API consumers."""
    try:
        response = await compile_scenario_agent(
            prompt=req.prompt,
            current_scenario=req.current_scenario,
            history=req.history,
        )
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
