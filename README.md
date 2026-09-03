# OpsAI

OpsAI is an enterprise AI operations platform. This repository contains the production-style foundation for the web application, API, shared contracts, and local infrastructure.

## Architecture

- `apps/web`: Next.js, React, TypeScript, Tailwind CSS frontend
- `apps/api`: FastAPI backend with SQLAlchemy and Alembic
- `packages/shared`: shared TypeScript constants and contracts
- `infra/docker`: Dockerfiles and Docker Compose for local services
- `docs`: architecture and project documentation

PostgreSQL is the primary database and enables the `pgvector` extension during the initial migration. Redis carries a small document-processing queue, and the worker extracts, chunks, and embeds uploaded files.

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

The API exposes `POST /auth/register`, `POST /auth/login`, `POST /auth/logout`, and
`GET /me`. Registration creates viewer accounts; authenticated role probes are
available at `/rbac/viewer`, `/rbac/analyst`, and `/rbac/admin`. Login and
registration set an HTTP-only authentication cookie.
Set `JWT_SECRET` to a long random value outside local development.

Document ingestion uses `POST /documents` (PDF, TXT, and Markdown), `GET /documents`
and `GET /documents/{id}`. `POST /documents/search` performs owner-scoped retrieval
over completed chunks. The API writes files below `STORAGE_DIR` using generated UUID
keys; it never uses client filenames as paths. The local embedding provider is
deterministic and requires no paid API. Set `EMBEDDING_PROVIDER=external`,
`EMBEDDING_API_URL`, and `EMBEDDING_API_KEY` to use a compatible embedding API.

The chat layer adds authenticated conversations and grounded RAG responses. Use
`POST /conversations` to create a chat, `GET /conversations` to list owned history,
and `POST /conversations/{conversation_id}/messages` for streaming responses with
SSE token/citation/done events. The server only retrieves chunks belonging to the
current user, keeps a bounded history window, and returns empty-context messaging
rather than hallucinating answers. Set `CHAT_PROVIDER=openai` and an API key to use
OpenAI Responses API; otherwise the local deterministic provider is used for tests
and offline development.

The local agent uses a typed deterministic intent parser and exposes the
allowlisted `search_knowledge`, `get_metric`, `get_incident`, and
`create_incident` tools. Metrics are read-only recorded values (including
payment failure rate, daily error rate, and transaction count); no metric is
invented when the database has no observation. Incident creation is proposal
only: analysts and administrators must approve an unexpired action before the
single persisted execution boundary can run it. Tool activity is streamed as
`tool_call`/`tool_result` SSE events; proposals emit `approval_required` and
never perform an external side effect.
Analysts and admins can approve and execute actions through `/actions/{id}/approve` and
`/actions/{id}/execute`; all changes are persisted and visible in `/audit-logs`.
`GET /metrics` and `POST /metrics/query` are read-only. A dependency-free,
authenticated JSON-RPC adapter is available at `POST /mcp` for `tools/list` and
`tools/call` over the same services and authorization rules. Redis rate limiting safely degrades when Redis is
unavailable.

Compose starts the separate `worker` service. Run it manually with `python -m app.worker`
after migrations.

Run migrations locally with:

```bash
cd apps/api
alembic upgrade head
```

The document schema is migration `0003_documents_rag`. After starting Compose,
the API and worker apply migrations automatically.

## Environment variables

See `.env.example` for the complete local configuration. `NEXT_PUBLIC_API_URL` is exposed to the browser; database, Redis, and JWT settings are backend-only.

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
