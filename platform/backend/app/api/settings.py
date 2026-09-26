"""Workspace provider preferences and local credential setup."""

import httpx
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import require_authenticated
from app.database import get_db
from app.models.entities import UserPreference
from app.models.user import User
from app.provider_settings import (
    PROVIDERS,
    credential_source,
    provider_key,
    remove_provider_key,
    resolve_provider,
    save_provider_key,
)

router = APIRouter(prefix="/settings", tags=["Settings"])


class SettingsUpdate(BaseModel):
    provider: str


class KeyRequest(BaseModel):
    key: str
    provider: str | None = None
    base_url: str | None = None


class KeySaveRequest(KeyRequest):
    provider: str
    model: str


class ModelUpdate(BaseModel):
    model: str


def _detect_prefix(key: str) -> str | None:
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("AIza"):
        return "gemini"
    if key.startswith("xai-"):
        return "grok"
    if key.startswith(("sk-proj-", "sk-svcacct-")):
        return "openai"
    return None


def _clean_key(key: str) -> str:
    cleaned = key.strip()
    if not 8 <= len(cleaned) <= 2048 or any(character.isspace() or ord(character) < 32 for character in cleaned):
        raise HTTPException(status_code=422, detail="Enter a valid API key.")
    return cleaned


def _validated_base_url(base_url: str | None) -> str:
    if not base_url:
        raise HTTPException(status_code=422, detail="Enter this service's OpenAI-compatible API base URL.")
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=422, detail="Enter a valid API base URL.")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "host.docker.internal"):
        raise HTTPException(status_code=422, detail="Use HTTPS for a remote API URL.")
    return base_url.strip().rstrip("/")


async def _available_models(provider: str, key: str, base_url: str | None = None) -> list[str]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=422, detail="Choose a supported provider.")
    endpoints = {
        "openai": "https://api.openai.com/v1/models",
        "anthropic": "https://api.anthropic.com/v1/models?limit=100",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
        "grok": "https://api.x.ai/v1/models",
        "openai_compatible": f"{_validated_base_url(base_url)}/models" if provider == "openai_compatible" else "",
    }
    headers = (
        {"x-api-key": key, "anthropic-version": "2023-06-01"} if provider == "anthropic" else
        {"x-goog-api-key": key} if provider == "gemini" else
        {"Authorization": f"Bearer {key}"}
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(endpoints[provider], headers=headers)
    except httpx.RequestError as error:
        raise HTTPException(status_code=503, detail=f"Could not reach {PROVIDERS[provider]['name']} to validate this key.") from error
    if response.status_code in (401, 403):
        raise HTTPException(status_code=422, detail=f"{PROVIDERS[provider]['name']} did not accept this key or allow model listing.")
    if not response.is_success:
        raise HTTPException(status_code=503, detail=f"{PROVIDERS[provider]['name']} could not validate this key right now.")
    try:
        entries = response.json().get("models" if provider == "gemini" else "data", [])
    except ValueError as error:
        raise HTTPException(status_code=503, detail="The provider returned an unreadable model list.") from error
    if provider == "gemini":
        models = [
            item["name"].removeprefix("models/") for item in entries
            if isinstance(item, dict) and isinstance(item.get("name"), str)
            and item["name"].startswith("models/gemini-")
            and "generateContent" in item.get("supportedGenerationMethods", [])
        ]
    elif provider == "openai_compatible":
        models = [item["id"] for item in entries if isinstance(item, dict) and isinstance(item.get("id"), str)]
    else:
        prefixes = {"openai": ("gpt-", "o1", "o3", "o4", "o5"), "anthropic": ("claude-",), "grok": ("grok-",)}[provider]
        models = [
            item["id"] for item in entries
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and item["id"].startswith(prefixes)
            and not any(part in item["id"] for part in ("image", "audio", "embedding", "tts", "realtime", "vision"))
        ]
    models = sorted(set(models))
    if not models:
        raise HTTPException(status_code=422, detail="This key has no supported text generation models available.")
    return models


def _recommended_model(provider: str, models: list[str]) -> str:
    default = PROVIDERS[provider]["model"]
    if default and default in models:
        return default
    for term in ("flash", "haiku", "mini", "sonnet"):
        matches = [model for model in models if term in model and "preview" not in model]
        if matches:
            return matches[-1]
    return models[-1]


def _preference(db: Session, user: User, key: str) -> UserPreference | None:
    return db.get(UserPreference, (user.id, key))


def _set_preference(db: Session, user: User, key: str, value: str) -> None:
    preference = _preference(db, user, key)
    if preference:
        preference.value = value
    else:
        db.add(UserPreference(user_id=user.id, key=key, value=value))


def _compatible_endpoint(db: Session, user: User) -> str | None:
    preference = _preference(db, user, "endpoint:openai_compatible")
    return preference.value if preference else None


def _settings_response(db: Session, user: User) -> dict:
    resolved = resolve_provider(db, user.id)
    return {
        "preferredProvider": resolved["preference"],
        "activeProvider": resolved["id"],
        "activeModel": resolved["model"],
        "providers": [
            {
                "id": name,
                "name": details["name"],
                "model": (_preference(db, user, f"model:{name}") or UserPreference(value=details["model"])).value,
                "configured": resolved["configured"][name],
                "source": credential_source(name, user.id, db),
                "baseUrl": _compatible_endpoint(db, user) if name == "openai_compatible" else None,
            }
            for name, details in PROVIDERS.items()
        ],
    }


@router.get("")
def get_settings(db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    return _settings_response(db, user)


@router.put("")
def update_settings(update: SettingsUpdate, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    if update.provider not in (*PROVIDERS, "automatic", "offline"):
        raise HTTPException(status_code=422, detail="Unknown provider.")
    if update.provider in PROVIDERS and not resolve_provider(db, user.id)["configured"][update.provider]:
        raise HTTPException(status_code=422, detail="Add a key for this provider before selecting it.")
    _set_preference(db, user, "provider", update.provider)
    db.commit()
    return _settings_response(db, user)


@router.post("/detect")
async def detect_key(request: KeyRequest):
    key = _clean_key(request.key)
    provider = request.provider or _detect_prefix(key)
    if provider is None:
        return {"needsProvider": True, "providers": [{"id": name, "name": details["name"]} for name, details in PROVIDERS.items()]}
    base_url = _validated_base_url(request.base_url) if provider == "openai_compatible" else None
    models = await _available_models(provider, key, base_url)
    return {"needsProvider": False, "provider": provider, "providerName": PROVIDERS[provider]["name"], "baseUrl": base_url, "models": models, "recommendedModel": _recommended_model(provider, models)}


@router.post("/credentials")
async def save_key(request: KeySaveRequest, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    key = _clean_key(request.key)
    base_url = _validated_base_url(request.base_url) if request.provider == "openai_compatible" else None
    models = await _available_models(request.provider, key, base_url)
    if request.model not in models:
        raise HTTPException(status_code=422, detail="Choose a model available to this key.")
    save_provider_key(db, user.id, request.provider, key)
    _set_preference(db, user, "provider", request.provider)
    _set_preference(db, user, f"model:{request.provider}", request.model)
    if base_url:
        _set_preference(db, user, "endpoint:openai_compatible", base_url)
    db.commit()
    return _settings_response(db, user)


@router.delete("/credentials/{provider}")
def delete_key(provider: str, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider.")
    if credential_source(provider, user.id, db) != "user":
        raise HTTPException(status_code=404, detail="No API key is saved for this account.")
    remove_provider_key(db, user.id, provider)
    if provider == "openai_compatible":
        endpoint = _preference(db, user, "endpoint:openai_compatible")
        if endpoint:
            db.delete(endpoint)
    preference = _preference(db, user, "provider")
    if preference and preference.value == provider:
        preference.value = "automatic"
    db.commit()
    return _settings_response(db, user)


@router.get("/models/{provider}")
async def list_models(provider: str, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider.")
    key = provider_key(provider, user.id, db)
    if not key:
        raise HTTPException(status_code=404, detail="No key is configured for this provider.")
    endpoint = _compatible_endpoint(db, user) if provider == "openai_compatible" else None
    models = await _available_models(provider, key, endpoint)
    return {"models": models, "recommendedModel": _recommended_model(provider, models)}


@router.put("/models/{provider}")
async def update_model(provider: str, update: ModelUpdate, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider.")
    key = provider_key(provider, user.id, db)
    if not key:
        raise HTTPException(status_code=404, detail="No key is configured for this provider.")
    endpoint = _compatible_endpoint(db, user) if provider == "openai_compatible" else None
    models = await _available_models(provider, key, endpoint)
    if update.model not in models:
        raise HTTPException(status_code=422, detail="Choose an available model.")
    _set_preference(db, user, f"model:{provider}", update.model)
    db.commit()
    return _settings_response(db, user)
