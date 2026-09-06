# OpsAI

OpsAI is a production-style AI operations platform demonstrating grounded
knowledge retrieval, read-only service metrics, and approval-gated incident
workflows. It is designed to run locally with deterministic providers and does
not require an OpenAI API key.

## Problem

Operations teams need answers grounded in internal documents and recorded
service data, while operational mutations must remain controlled, auditable,
and permission-aware.

## Solution

OpsAI combines an authenticated Next.js frontend with a FastAPI backend, an
owner-scoped RAG pipeline, typed allowlisted tools, RBAC, approval gates,
idempotent action execution, audit logs, Redis rate limiting, and an
authenticated MCP-style JSON-RPC adapter.

## Architecture

```text
Browser
  -> Next.js frontend
  -> FastAPI API
  -> controlled deterministic agent
  -> typed allowlisted tools
  -> RAG / metrics / incidents
  -> approval gate
  -> audit log
  -> PostgreSQL + pgvector
  -> Redis
```

- `apps/web`: Next.js, React, TypeScript, Tailwind CSS, and SSE chat UI
- `apps/api`: FastAPI, SQLAlchemy, Alembic, authentication, agent, tools, and workflows
- `packages/shared`: intentionally small shared TypeScript contracts
- `infra/docker`: PostgreSQL/pgvector, Redis, API, worker, and web containers
- `docs`: architecture documentation

PostgreSQL is the durable system of record and stores documents, chunks,
conversations, metrics, incidents, approvals, and audit events. Redis carries
document-processing jobs and supports rate limiting. Uploaded files use generated
storage keys outside the source tree.

## Security and control boundaries

The API owns validation, authorization, business logic, and persistence. The
frontend never decides permissions. Viewer users cannot propose incidents;
analysts and administrators can propose; administrators alone approve and
execute. Mutating actions require an unexpired approval and are protected by
database-backed idempotency.

The four tools are:

| Tool | Purpose | Mutation |
| --- | --- | --- |
| `search_knowledge` | Owner-scoped document retrieval | No |
| `get_metric` | Read an allowlisted recorded metric | No |
| `get_incident` | Read an authorized incident | No |
| `create_incident` | Create an approval-required proposal | Proposal only |

Tool names and arguments are validated against an allowlist. Every important
workflow writes audit events. Authentication uses HTTP-only cookies, request
IDs are correlated in logs, and Redis rate limiting fails safely according to
its documented local-development behavior.

The API exposes an authenticated **MCP-style JSON-RPC adapter** at `POST /mcp`.
It reuses the same service and tool implementations; it is not described as a
standards-certified MCP server.

## Agent and approval workflow

The local agent uses typed deterministic intent routing. Knowledge requests use
owner-scoped retrieval and similarity thresholding. Metric requests only read
persisted allowlisted values. Incident requests create proposals and stream
`approval_required` activity; no external side effect occurs before approval.

## Demo flow

1. Register and upload a PDF, TXT, or Markdown document.
2. Ask a grounded question about the uploaded document and inspect citations.
3. Ask a metric question such as “What is the latency?”
4. Confirm a viewer is denied when requesting an incident mutation.
5. Use an analyst account to create an incident proposal.
6. Use an administrator account to approve the action.
7. Execute the approved action as an administrator.
8. Inspect the resulting audit record through the API.

## Evaluation and observability

The offline evaluator exercises deterministic behavior for knowledge context
gating, metrics, incident lifecycle and idempotency, RBAC, and MCP requests:

```bash
cd apps/api
python -m app.evaluation
```

It reports total cases, passed, failed, pass rate, category, case name,
expected behavior, and actual behavior. This is regression evaluation, not LLM
quality scoring. Structured logs include request IDs, operation/tool, user,
status, resource identifiers, and useful durations without logging credentials,
tokens, cookies, API keys, or document contents.

`/health` is a cheap liveness/database check. `/ready` checks database and Redis
availability and is suitable for dependency-aware routing decisions.

## Local development

1. Copy the safe template and replace placeholders:

   ```bash
   cp .env.example .env
   ```

2. Set a long random `JWT_SECRET`. Keep `AUTH_COOKIE_SECURE=false` only for
   local HTTP development; use `true` behind HTTPS.

3. Start the stack:

   ```bash
   docker compose --env-file .env -f infra/docker/docker-compose.yml up --build
   ```

4. Open:

   - Web: http://localhost:3000
   - API docs: http://localhost:8000/docs
   - Liveness: http://localhost:8000/health
   - Readiness: http://localhost:8000/ready

Run migrations manually when developing the API outside Compose:

```bash
cd apps/api
alembic upgrade head
```

## Testing and CI/CD

Backend:

```bash
cd apps/api
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check app tests
ruff format --check app tests
mypy app
```

Frontend:

```bash
npm ci
npm run test:web
npm run lint:web
npm run typecheck:web
npm run build:web
```

GitHub Actions in `.github/workflows/ci.yml` runs these backend and frontend
checks on every push and pull request. CI uses local deterministic behavior and
requires no paid AI service, API key, or external runtime dependency.

## Key engineering decisions

- deterministic local providers keep development and CI reproducible
- retrieval is owner-scoped and thresholded before citations are emitted
- typed allowlists prevent arbitrary tool or SQL routing
- approval-before-mutation separates proposal from execution
- database locking and idempotency protect repeated actions
- audit logs and request-correlated structured logs support investigation
- the MCP-style adapter reuses existing authorization and service boundaries
- the evaluation suite tests behavior rather than inventing model-quality scores

## Limitations

The local provider is deterministic and intentionally limited; it is not a
general-purpose production LLM. Incident execution is a persisted local
execution boundary and does not integrate with external ticketing or cloud
operations systems. The repository includes practical local Docker and CI
preparation, but deployment, secret management, TLS termination, backups,
scaling, and production monitoring remain infrastructure responsibilities.

## Configuration

`.env.example` contains placeholders only. `.env` is ignored by Git. Database,
Redis, JWT, cookie, storage, retrieval, provider, and frontend API settings are
configurable through environment variables. The local provider defaults remain
safe for offline development.
