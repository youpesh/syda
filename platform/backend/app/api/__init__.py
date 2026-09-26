from app.api.agent import router as agent_router
from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.generation import router as generation_router
from app.api.scenarios import router as scenarios_router
from app.api.settings import router as settings_router

__all__ = ["agent_router", "auth_router", "conversations_router", "generation_router", "scenarios_router", "settings_router"]
