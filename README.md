# OpsAI

OpsAI is an enterprise AI operations platform. This repository contains the production-style foundation for the web application, API, shared contracts, and local infrastructure.

## Architecture

- `apps/web`: Next.js, React, TypeScript, Tailwind CSS frontend
- `apps/api`: FastAPI backend with SQLAlchemy and Alembic
- `packages/shared`: shared TypeScript constants and contracts
- `infra/docker`: Dockerfiles and Docker Compose for local services
- `docs`: architecture and project documentation

PostgreSQL is the primary database and enables the `pgvector` extension during the initial migration. Redis is available for future background work and caching.

## Local setup

1. Copy the environment template:

   ```bash
   cp .env.example .env
   ```

2. Start the complete local stack:

   ```bash
   docker compose -f infra/docker/docker-compose.yml up --build
   ```

3. Open:

   - Web: http://localhost:3000
   - API docs: http://localhost:8000/docs
   - API health: http://localhost:8000/health

Run migrations locally with:

```bash
cd apps/api
alembic upgrade head
```

## Environment variables

See `.env.example` for the complete local configuration. `NEXT_PUBLIC_API_URL` is exposed to the browser; database and Redis connection strings are backend-only.

## Development commands

```bash
# Frontend
npm install
npm run dev:web
npm run lint:web
npm run typecheck:web
npm run test:web

# Backend
cd apps/api
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
mypy app
```

## Checks

```bash
npm run lint
npm run typecheck
npm run test
```

See [the architecture diagram](docs/architecture.md) for the service boundaries and local dependencies.
