# Syda Platform

An end-to-end web platform and interactive studio built directly into the [Syda](https://github.com/syda-ai/syda) monorepo.

Syda Platform provides an interactive visual studio to define data schemas, configure causal constraints, preview generated synthetic datasets, and run generation workloads with referential integrity.

---

## Monorepo Architecture

```text
syda/
├── syda/                 # Core Python library (SDK & CLI)
├── tests/                # Core library pytest suite
├── docs/                 # Documentation
├── pyproject.toml        # Core package configuration
└── platform/             # Web Platform & Interactive Studio
    ├── backend/          # FastAPI REST & streaming API service
    │   ├── app/          # API routes, scenario agent, generation models
    │   └── pyproject.toml # Configured with syda = { path = "../..", editable = true }
    ├── frontend/         # React Router 7 + Tailwind CSS studio interface
    │   ├── app/          # Routes, UI components, state management
    │   └── public/       # Client static assets
    ├── scripts/
    │   └── setup.sh      # Automated environment installer
    └── package.json      # Monorepo orchestrator (npm run dev)
```

---

## Tech Stack

- **Frontend Studio**: [React 19](https://react.dev/), [React Router 7](https://reactrouter.com/), [shadcn/ui](https://ui.shadcn.com/) with Base UI, [Tailwind CSS v4](https://tailwindcss.com/), [Vite](https://vitejs.dev/), and [Hugeicons](https://hugeicons.com/)
- **Backend API**: [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [Pydantic AI](https://ai.pydantic.dev/)
- **Core Engine**: [Syda](https://github.com/syda-ai/syda) (linked directly in editable mode from the parent repository)

## MVP Workflow

1. Describe a dataset in natural language and receive a structured scenario.
2. Review and edit its tables, fields, workflow steps, and causal rules.
3. Inspect the generation cost estimate and run preflight validation.
4. Generate while following backend-reported progress.
5. Preview each table and download CSV or JSON output.
6. Review measured quality gates and export the evaluation report as JSON or HTML.

---

## Quick Start

### Prerequisites
- **Node.js**: >= 20
- **Python**: >= 3.11 (or [uv](https://docs.astral.sh/uv/))

### 1. Setup

From the root of the repository:
```bash
cd platform
npm run setup
```

This will automatically:
1. Sync backend Python dependencies with `uv` (or `pip`) and link the parent `syda` core in editable mode (`-e ../..`).
2. Install frontend and platform npm packages.

### 2. Run Development Server

```bash
cd platform
npm run dev
```

This starts both services concurrently with unified logs:
- **Frontend Studio**: `http://localhost:5173`
- **Backend API**: `http://localhost:8000` (proxied from frontend via `/api` and `/health`)

---

## Available Commands

From inside `platform/`:

| Command | Description |
| :--- | :--- |
| `npm run setup` | Configures Python environment, links core syda, installs npm packages |
| `npm run dev` | Starts frontend & backend concurrently with live reload |
| `npm run dev:backend` | Starts FastAPI backend server only (`uvicorn`) |
| `npm run dev:frontend` | Starts React Router Vite server only |
| `npm run typecheck` | Validates TypeScript types and React Router routing |
| `npm run build` | Builds frontend production bundles |

---

## Running with Docker & PostgreSQL

For a fully containerized stack including a dedicated PostgreSQL 16 database:

```bash
cd platform
docker compose up -d
```

This starts:
- **PostgreSQL 16**: `localhost:5432` (database `syda`, user `postgres`)
- **FastAPI Backend**: `http://localhost:8000` (pre-configured with `DATABASE_URL`)
- **Frontend Studio**: `http://localhost:3000`

To stop the containers:
```bash
docker compose down
```

## Queued dataset generation

Dataset generation runs in a Celery worker. FastAPI creates a PostgreSQL job in
`queued` state and publishes its ID to Redis. A dispatcher republishes unclaimed
queued jobs and recovers expired worker leases every five seconds. PostgreSQL is
the authoritative job/result store; Redis only transports task IDs.

`docker compose up --build` starts the API, frontend, PostgreSQL, Redis, worker,
and dispatcher. The worker uses the same backend image and job/settings volumes
as the API. Redis is internal to the Compose network and persists its queue using
AOF. For queue deployments use PostgreSQL; SQLite remains useful for unit tests.

For development, after `uv sync --project platform/backend`, run the following in
separate terminals from `platform/backend`, alongside the frontend and API:

```bash
uv run celery -A app.celery_app:celery_app worker --loglevel=INFO --concurrency=1 --queues=generation
uv run python -m app.dispatcher
```

Set `CELERY_BROKER_URL` to a running Redis instance and `DATABASE_URL` to the same
PostgreSQL database in all processes. The API must initialize the schema before
starting the worker and dispatcher. All processes must share `JOB_DATA_DIR` and
`PROVIDER_ENCRYPTION_KEY` (or the existing `data/settings` directory). Keep one
API instance during the additive schema migration, then start other instances.
Stop/drain old API-owned generations before upgrading; legacy interrupted jobs
without a saved provider snapshot are marked failed by reconciliation.

The provider, model, and compatible endpoint are captured at submission. API keys
are never put in Redis: the worker decrypts the owner's current saved key. Key
rotation before execution therefore takes effect; deleting a required credential
fails the job rather than switching providers. Live provider failures now surface
as failed/retrying jobs instead of silently substituting offline data.

Generation attempts have an atomic claim, a heartbeat, and a unique output
directory. Only the current attempt can update status or publish downloads.
Unfinished output stays private; legacy completed output paths remain supported.
An API restart does not mark worker-owned jobs failed. A worker whose heartbeat
expires is retried from the beginning, up to `GENERATION_MAX_ATTEMPTS` (default 3).
Explicit timeout/connection errors, HTTP 429, and HTTP 5xx failures are retried with
bounded backoff. Invalid input and missing credentials fail immediately.

Default maximum task duration is one hour (`GENERATION_TIME_LIMIT`); Redis's
visibility timeout is set longer than that duration. `GENERATION_LEASE_SECONDS`
defaults to 120 seconds, with a heartbeat every third of that interval. A long
visibility timeout does not block database reconciliation of a lost worker.
Repeated delivery never starts a second attempt while an existing lease is live.
Fencing prevents stale attempts from publishing results, but cannot undo a model
API call already in flight: recovery can incur additional provider usage. This is
at-least-once execution, not exactly-once billing or partial-generation resume.
Only increase concurrency after checking memory and provider rate limits.

Validation:

```bash
uv run --project platform/backend pytest platform/backend/tests tests/test_scenarios.py
SYDA_QUEUE_INTEGRATION=1 uv run --project platform/backend pytest platform/backend/tests/test_celery_transport.py -v
```

The opt-in test requires disposable PostgreSQL and Redis services and exercises
real broker delivery, duplicate task delivery, offline generation, and output
publication. CI provisions these services. Before production rollout also stop a
worker during generation, restart the API independently, and verify recovery and
downloads from the API container. Cleanup of old attempt directories, cancellation,
priorities, and resuming partial generation are outside this change.
