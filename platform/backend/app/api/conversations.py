"""Per-user conversation history for AI-assisted scenario design."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_authenticated
from app.database import get_db
from app.models.entities import ChatConversation
from app.models.scenario import ScenarioConfiguration
from app.models.user import User

router = APIRouter(prefix="/conversations", tags=["Conversations"])


class ConversationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    messages: list[dict[str, Any]] | None = None
    runs: list[dict[str, Any]] | None = None
    scenario_draft: ScenarioConfiguration | None = Field(default=None, alias="scenarioDraft")


def _owned_conversation(db: Session, conversation_id: str, user: User) -> ChatConversation:
    conversation = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.id == conversation_id,
            ChatConversation.user_id == user.id,
        )
        .first()
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.get("")
def list_conversations(
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
    limit: int = Query(50, ge=1, le=100),
):
    conversations = (
        db.query(ChatConversation)
        .filter(ChatConversation.user_id == user.id)
        .order_by(ChatConversation.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [conversation.to_dict(include_content=False) for conversation in conversations]


@router.post("", status_code=201)
def create_conversation(
    request: ConversationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    conversation = ChatConversation(
        id=str(uuid.uuid4()),
        user_id=user.id,
        title=request.title.strip() or "New conversation",
        messages=[],
        runs=[],
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation.to_dict()


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    return _owned_conversation(db, conversation_id, user).to_dict()


@router.put("/{conversation_id}")
def update_conversation(
    conversation_id: str,
    request: ConversationUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    conversation = _owned_conversation(db, conversation_id, user)
    if request.title is not None:
        conversation.title = request.title.strip() or conversation.title
    if request.messages is not None:
        conversation.messages = request.messages
    if request.runs is not None:
        conversation.runs = request.runs
    if "scenario_draft" in request.model_fields_set and request.scenario_draft is not None:
        draft = request.scenario_draft.model_dump(by_alias=True)
        if conversation.scenario_draft != draft:
            conversation.scenario_draft = draft
            conversation.draft_version = (conversation.draft_version or 0) + 1
    conversation.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(conversation)
    return conversation.to_dict(include_content=False)


@router.delete("/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    conversation = _owned_conversation(db, conversation_id, user)
    db.delete(conversation)
    db.commit()
