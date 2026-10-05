from .agent import router as agent_router
from .auth import router as auth_router
from .conversations import router as conversations_router
from .generation import router as generation_router
from .scenarios import router as scenarios_router
from .settings import router as settings_router

__all__ = ["agent_router", "auth_router", "conversations_router", "generation_router", "scenarios_router", "settings_router"]
