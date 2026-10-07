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
