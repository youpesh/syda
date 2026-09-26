import os
from dotenv import load_dotenv

load_dotenv(override=True)
load_dotenv("backend/.env", override=True)

from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(
    title="Syda Platform API",
    version="0.1.0",
    lifespan=lifespan,
)

origins_env = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173")
allowed_origins = [origin.strip() for origin in origins_env.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api import agent_router, auth_router, conversations_router, generation_router, scenarios_router, settings_router
from app.api.auth import require_authenticated

app.include_router(auth_router, prefix="/api")
app.include_router(auth_router)

app.include_router(conversations_router, prefix="/api", dependencies=[Depends(require_authenticated)])
app.include_router(agent_router, prefix="/api", dependencies=[Depends(require_authenticated)])
app.include_router(generation_router, prefix="/api", dependencies=[Depends(require_authenticated)])
app.include_router(scenarios_router, prefix="/api", dependencies=[Depends(require_authenticated)])
app.include_router(settings_router, prefix="/api", dependencies=[Depends(require_authenticated)])

# Also include directly for direct REST API callers without /api prefix
app.include_router(conversations_router, dependencies=[Depends(require_authenticated)])
app.include_router(agent_router, dependencies=[Depends(require_authenticated)])
app.include_router(generation_router, dependencies=[Depends(require_authenticated)])
app.include_router(scenarios_router, dependencies=[Depends(require_authenticated)])
app.include_router(settings_router, dependencies=[Depends(require_authenticated)])

@app.get("/health")
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "syda-platform-api",
    }
