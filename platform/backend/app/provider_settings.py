"""Per-user model preferences and encrypted provider credentials."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.entities import (
    JobRecord,
    ScenarioRecord,
    UserPreference,
    UserProviderCredential,
    WorkspacePreference,
)

PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {"name": "OpenAI", "model": "gpt-4o-mini"},
    "anthropic": {"name": "Anthropic", "model": "claude-haiku-4-5-20251001"},
    "gemini": {"name": "Google Gemini", "model": "gemini-2.5-flash"},
    "grok": {"name": "xAI", "model": "grok-4"},
    "openai_compatible": {"name": "Other compatible API", "model": ""},
}

SETTINGS_DIR = Path(__file__).resolve().parents[1] / "data" / "settings"
LEGACY_KEYS_PATH = SETTINGS_DIR / "provider_keys.json"
FERNET_KEY_PATH = SETTINGS_DIR / "provider_fernet.key"


def _fernet() -> Fernet:
    configured = os.getenv("PROVIDER_ENCRYPTION_KEY")
    if configured:
        return Fernet(configured.encode())
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        key = FERNET_KEY_PATH.read_bytes().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        descriptor, name = tempfile.mkstemp(prefix=".provider-fernet-", dir=SETTINGS_DIR)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(key)
            os.chmod(name, 0o600)
            os.replace(name, FERNET_KEY_PATH)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return Fernet(key)


def provider_key(provider: str, user_id: UUID | None = None, db: Session | None = None) -> str | None:
    if user_id is None or db is None:
        return None
    credential = db.get(UserProviderCredential, (user_id, provider))
    if not credential:
        return None
    try:
        return _fernet().decrypt(credential.encrypted_key.encode()).decode()
    except (InvalidToken, UnicodeDecodeError) as error:
        raise RuntimeError("Could not decrypt the saved provider key. Check PROVIDER_ENCRYPTION_KEY.") from error


def credential_source(provider: str, user_id: UUID | None = None, db: Session | None = None) -> str | None:
    if user_id is not None:
        return "user" if db is not None and db.get(UserProviderCredential, (user_id, provider)) else None
    return None


def save_provider_key(db: Session, user_id: UUID, provider: str, key: str) -> None:
    record = db.get(UserProviderCredential, (user_id, provider))
    encrypted_key = _fernet().encrypt(key.encode()).decode()
    if record:
        record.encrypted_key = encrypted_key
    else:
        db.add(UserProviderCredential(user_id=user_id, provider=provider, encrypted_key=encrypted_key))


def remove_provider_key(db: Session, user_id: UUID, provider: str) -> None:
    record = db.get(UserProviderCredential, (user_id, provider))
    if record:
        db.delete(record)


def configured_providers(db: Session, user_id: UUID) -> dict[str, bool]:
    return {provider: bool(provider_key(provider, user_id, db)) for provider in PROVIDERS}


def _preference(db: Session, user_id: UUID | None, name: str) -> UserPreference | WorkspacePreference | None:
    if user_id is not None:
        return db.get(UserPreference, (user_id, name))
    # Keep no-user calls useful for internal tools that only use server settings.
    return db.get(WorkspacePreference, name)


def provider_preference(db: Session, user_id: UUID | None = None) -> str:
    preference = _preference(db, user_id, "provider")
    return preference.value if preference and preference.value in (*PROVIDERS, "automatic", "offline") else "automatic"


def resolve_provider(db: Session | None = None, user_id: UUID | None = None) -> dict[str, Any]:
    own_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        preference = provider_preference(db, user_id)
        endpoint_record = _preference(db, user_id, "endpoint:openai_compatible")
        endpoint = endpoint_record.value if endpoint_record and user_id is not None else None
        models = {
            name: (_preference(db, user_id, f"model:{name}") or WorkspacePreference(value=details["model"])).value
            for name, details in PROVIDERS.items()
        }
        configured = {name: bool(provider_key(name, user_id, db)) for name in PROVIDERS}
        configured["openai_compatible"] = configured["openai_compatible"] and bool(endpoint)
        if preference == "offline":
            selected = "offline"
        elif preference in PROVIDERS and configured[preference]:
            selected = preference
        else:
            selected = next((name for name in PROVIDERS if configured[name]), "offline")
        if selected == "offline":
            return {"id": selected, "name": "Offline", "model": "Local schema synthesizer", "agent_model": None, "preference": preference, "configured": configured}
        model = models[selected]
        agent_prefix = "google" if selected == "gemini" else ("openai" if selected in ("grok", "openai_compatible") else selected)
        return {
            "id": selected,
            "name": PROVIDERS[selected]["name"],
            "model": model,
            "agent_model": f"{agent_prefix}:{model}",
            "key": provider_key(selected, user_id, db),
            "base_url": endpoint if selected == "openai_compatible" else None,
            "preference": preference,
            "configured": configured,
        }
    finally:
        if own_session:
            db.close()


def adopt_legacy_shared_data(db: Session, user_id: UUID) -> None:
    """Assign pre-auth shared rows and settings to the first account, without losing them."""
    db.query(ScenarioRecord).filter(ScenarioRecord.user_id.is_(None)).update(
        {ScenarioRecord.user_id: user_id}, synchronize_session=False
    )
    db.query(JobRecord).filter(JobRecord.user_id.is_(None)).update(
        {JobRecord.user_id: user_id}, synchronize_session=False
    )
    for old_preference in db.query(WorkspacePreference).all():
        if not db.get(UserPreference, (user_id, old_preference.key)):
            db.add(UserPreference(user_id=user_id, key=old_preference.key, value=old_preference.value))

    legacy_keys: dict[str, str] = {}
    try:
        raw_keys = json.loads(LEGACY_KEYS_PATH.read_text())
        if isinstance(raw_keys, dict):
            legacy_keys = {name: value for name, value in raw_keys.items() if name in PROVIDERS and isinstance(value, str)}
    except (FileNotFoundError, ValueError, OSError):
        pass
    for provider, key in legacy_keys.items():
        if key and not db.get(UserProviderCredential, (user_id, provider)):
            db.add(UserProviderCredential(
                user_id=user_id,
                provider=provider,
                encrypted_key=_fernet().encrypt(key.encode()).decode(),
            ))
    db.query(WorkspacePreference).delete(synchronize_session=False)
    db.commit()
    if legacy_keys:
        LEGACY_KEYS_PATH.unlink(missing_ok=True)
