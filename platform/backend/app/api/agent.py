import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_authenticated
from app.database import get_db
from app.models.entities import ChatConversation
from app.models.scenario import ChatRequest, ChatResponse, ScenarioConfiguration
from app.models.user import User
from app.agents.scenario_agent import compile_scenario_agent
from app.provider_settings import resolve_provider

router = APIRouter(prefix="/agent", tags=["Agent"])
logger = logging.getLogger(__name__)

class ChatStreamRequest(BaseModel):
    """The AI SDK UIMessage request shape sent by useChat or standard chat clients."""

    messages: list[dict[str, Any]] = Field(default_factory=list)
    prompt: str | None = None
    message: str | dict[str, Any] | None = None
    id: str | None = None
    chatId: str | None = None
    conversationId: str | None = None


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
                    scenario = ScenarioConfiguration.model_validate(part["data"])
                    # Old chat turns may contain an incomplete artifact (for
                    # example, one created in response to "hello"). Never
                    # let that become the active draft for future turns.
                    if (
                        len(scenario.workflow) >= 2
                        and bool(scenario.rules)
                        and bool(scenario.schemas)
                        and all(isinstance(table, dict) and bool(table) for table in scenario.schemas.values())
                    ):
                        return scenario
                except Exception:
                    pass
        for key in ("scenario", "data"):
            data = message.get(key)
            if isinstance(data, dict):
                try:
                    scenario = ScenarioConfiguration.model_validate(data)
                    if (
                        len(scenario.workflow) >= 2
                        and bool(scenario.rules)
                        and bool(scenario.schemas)
                        and all(isinstance(table, dict) and bool(table) for table in scenario.schemas.values())
                    ):
                        return scenario
                except Exception:
                    pass
    return None


def _scenario_is_valid_draft(scenario: ScenarioConfiguration | None) -> bool:
    return bool(
        scenario
        and len(scenario.workflow) >= 2
        and scenario.rules
        and scenario.schemas
        and all(isinstance(table, dict) and table for table in scenario.schemas.values())
    )


def _sse(chunk: dict[str, Any]) -> str:
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


@router.post("/chat")
async def chat_with_agent(
    req: ChatStreamRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
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
    provider_selection = resolve_provider(db, user.id)
    conversation = None
    if req.conversationId:
        conversation = (
            db.query(ChatConversation)
            .filter(
                ChatConversation.id == req.conversationId,
                ChatConversation.user_id == user.id,
            )
            .first()
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

    current_scenario = None
    if conversation and conversation.scenario_draft:
        try:
            saved_draft = ScenarioConfiguration.model_validate(conversation.scenario_draft)
            if _scenario_is_valid_draft(saved_draft):
                current_scenario = saved_draft
        except Exception:
            logger.warning("Ignoring invalid saved draft for conversation %s", conversation.id)

    if current_scenario is None:
        current_scenario = _latest_scenario(messages[:-1])
        # One-time migration for existing chats whose latest valid draft only
        # exists as a data-scenario part in the transcript.
        if conversation and current_scenario:
            conversation.scenario_draft = current_scenario.model_dump(by_alias=True)
            conversation.draft_version = (conversation.draft_version or 0) + 1
            conversation.updated_at = datetime.utcnow()
            db.commit()

    async def stream():
        try:
            response = await compile_scenario_agent(
                prompt=prompt,
                current_scenario=current_scenario,
                history=history,
                provider_selection=provider_selection,
            )
            if conversation and _scenario_is_valid_draft(response.scenario):
                serialized_draft = response.scenario.model_dump(by_alias=True)
                if conversation.scenario_draft != serialized_draft:
                    conversation.scenario_draft = serialized_draft
                    conversation.draft_version = (conversation.draft_version or 0) + 1
                    conversation.updated_at = datetime.utcnow()
                    db.commit()
            text_id = "scenario-response"
            yield _sse({"type": "start"})
            yield _sse({"type": "text-start", "id": text_id})
            for delta in re.findall(r"\S+\s*", response.message):
                if await request.is_disconnected():
                    return
                yield _sse({"type": "text-delta", "id": text_id, "delta": delta})
                await asyncio.sleep(0.015)
            yield _sse({"type": "text-end", "id": text_id})
            if response.scenario is not None:
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
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-vercel-ai-ui-message-stream": "v1",
        },
    )


@router.post("/chat/complete", response_model=ChatResponse)
async def complete_chat(
    req: ChatRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    """Non-streaming compatibility endpoint for direct API consumers."""
    try:
        response = await compile_scenario_agent(
            prompt=req.prompt,
            current_scenario=req.current_scenario,
            history=req.history,
            provider_selection=resolve_provider(db, user.id),
        )
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
