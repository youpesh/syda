import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, TypeGuard

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import require_authenticated
from ..database import SessionLocal, get_db
from ..job_queue import DATA_DIR, output_directory
from ..models.entities import ChatConversation, JobRecord
from ..models.scenario import ChatRequest, ChatResponse, ScenarioConfiguration
from ..models.user import User
from ..agents.scenario_agent import compile_scenario_agent
from ..provider_settings import resolve_provider

router = APIRouter(prefix="/agent", tags=["Agent"])
logger = logging.getLogger(__name__)
CHAT_HEARTBEAT_SECONDS = 15
# Keep background work alive after its HTTP stream disconnects. Tasks are removed
# on completion; navigating away must not cancel draft persistence.
_compilation_tasks: set[asyncio.Task] = set()


def _compilation_finished(task: asyncio.Task) -> None:
    _compilation_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Background scenario compilation failed", exc_info=task.exception())

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
            return str(parts_text.strip())

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
            return str(list_text.strip())

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


def _scenario_is_valid_draft(scenario: ScenarioConfiguration | None) -> TypeGuard[ScenarioConfiguration]:
    return bool(
        scenario
        and len(scenario.workflow) >= 2
        and scenario.rules
        and scenario.schemas
        and all(isinstance(table, dict) and table for table in scenario.schemas.values())
    )


def _latest_dataset_dir(db: Session, conversation: ChatConversation, user_id: Any) -> str | None:
    """Output folder of the conversation's newest completed run, for dataset Q&A."""
    for run in reversed(conversation.runs or []):
        job_id = run.get("jobId") if isinstance(run, dict) else None
        if not isinstance(job_id, str):
            continue
        job = (
            db.query(JobRecord)
            .filter(JobRecord.id == job_id, JobRecord.user_id == user_id, JobRecord.status == "complete")
            .first()
        )
        if job:
            directory = output_directory(job, DATA_DIR)
            if any(directory.glob("*.csv")):
                return str(directory)
    return None


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
        # Persist the user message right away so it is immediately visible in history
        conversation.messages = messages
        conversation.updated_at = datetime.now(timezone.utc)
        db.commit()

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
            conversation.updated_at = datetime.now(timezone.utc)
            db.commit()

    conv_id = conversation.id if conversation else None
    user_id = user.id
    dataset_dir = _latest_dataset_dir(db, conversation, user.id) if conversation else None
    # Sync tools run in a worker thread, so hand status updates back to this loop.
    loop = asyncio.get_running_loop()
    status_queue: asyncio.Queue[str] = asyncio.Queue()

    def report_status(text: str) -> None:
        loop.call_soon_threadsafe(status_queue.put_nowait, text)

    async def run_compilation_and_persist():
        response = await compile_scenario_agent(
            prompt=prompt,
            current_scenario=current_scenario,
            history=history,
            provider_selection=provider_selection,
            dataset_dir=dataset_dir,
            on_status=report_status,
        )
        if conv_id:
            try:
                with SessionLocal() as session:
                    target_conv = (
                        session.query(ChatConversation)
                        .filter(
                            ChatConversation.id == conv_id,
                            ChatConversation.user_id == user_id,
                        )
                        .first()
                    )
                    if target_conv:
                        if response.scenario and _scenario_is_valid_draft(response.scenario):
                            serialized_draft = response.scenario.model_dump(by_alias=True)
                            if target_conv.scenario_draft != serialized_draft:
                                target_conv.scenario_draft = serialized_draft
                                target_conv.draft_version = (target_conv.draft_version or 0) + 1

                        assistant_parts: list[dict[str, Any]] = [
                            {"type": "text", "text": response.message, "state": "done"}
                        ]
                        if response.scenario is not None:
                            assistant_parts.append({
                                "type": "data-scenario",
                                "id": "scenario",
                                "data": response.scenario.model_dump(by_alias=True),
                            })
                        assistant_msg = {
                            "id": f"asst-{uuid.uuid4().hex[:12]}",
                            "role": "assistant",
                            "parts": assistant_parts,
                        }
                        cur_messages = list(target_conv.messages or messages)
                        already_present = any(
                            m.get("role") == "assistant"
                            and any(
                                p.get("text") == response.message
                                for p in m.get("parts", [])
                                if isinstance(p, dict) and p.get("type") == "text"
                            )
                            for m in cur_messages
                        )
                        if not already_present:
                            cur_messages.append(assistant_msg)
                        target_conv.messages = cur_messages
                        target_conv.updated_at = datetime.now(timezone.utc)
                        session.commit()
            except Exception as persist_err:
                logger.error("Failed to persist assistant turn for %s: %s", conv_id, persist_err)
        return response

    # Launch background compilation so it completes and persists even if client navigates away
    compilation_task = asyncio.create_task(run_compilation_and_persist())
    _compilation_tasks.add(compilation_task)
    compilation_task.add_done_callback(_compilation_finished)

    async def stream():
        try:
            # Flush the response immediately, then keep it active while the model
            # works. SSE comments are ignored by the AI SDK's event parser.
            yield ": keep-alive\n\n"
            while not compilation_task.done():
                if await request.is_disconnected():
                    return
                next_status = asyncio.ensure_future(status_queue.get())
                try:
                    done, _ = await asyncio.wait(
                        {compilation_task, next_status},
                        timeout=CHAT_HEARTBEAT_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not next_status.done():
                        next_status.cancel()
                if next_status in done:
                    # Transient: shown live by useChat's onData, never saved to the transcript.
                    yield _sse({"type": "data-status", "data": {"message": next_status.result()}, "transient": True})
                elif not done:
                    yield ": keep-alive\n\n"
            if await request.is_disconnected():
                return
            response = compilation_task.result()
            text_id = "scenario-response"
            yield _sse({"type": "start"})
            yield _sse({"type": "text-start", "id": text_id})
            yield _sse({"type": "text-delta", "id": text_id, "delta": response.message})
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
