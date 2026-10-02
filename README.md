# OpsAI

OpsAI is a production-style AI operations platform: grounded answers from your own
documents, read-only service metrics, and approval-gated incident workflows. It runs
locally with deterministic providers and does not require an OpenAI API key.

## Problem

Operations teams need answers grounded in internal documents and recorded service data,
while operational changes must stay controlled, auditable, and permission-aware.

## Solution

An authenticated Next.js frontend talks to a FastAPI backend with an owner-scoped RAG
pipeline, a deterministic agent that can only call typed allowlisted tools, RBAC,
proposal → approval → execution for mutations, idempotent execution, audit logs, Redis
rate limiting, structured request-correlated logs, and an authenticated MCP-style
JSON-RPC adapter.

## Architecture

```text
Browser (Next.js, SSE)
  -> FastAPI routes          auth, validation, HTTP semantics, SSE framing
  -> chat orchestration      app/chat.py: routes each message to the agent or RAG
  -> deterministic agent     app/agent.py: typed intents, no free-form tool calls
  -> allowlisted tools       app/tools.py: search_knowledge, get_metric, get_incident, create_incident
  -> domain services         app/retrieval.py, app/incidents.py (approval gate, idempotency, audit)
  -> PostgreSQL + pgvector   system of record; Redis for the job queue and rate limits
```

- `apps/web`: Next.js, React, TypeScript, Tailwind CSS
- `apps/api`: FastAPI, SQLAlchemy, Alembic, agent, tools, worker, evaluation
- `packages/shared`: small shared TypeScript constants
- `infra/docker`: PostgreSQL/pgvector, Redis, API, worker, and web containers
- `docs/architecture.md`: boundaries, request flows, and the approval state machine

## Security and control boundaries

The API owns validation, authorization, business logic, and persistence; the frontend
only hides controls a user cannot use. Roles:

| Capability | Viewer | Analyst | Admin |
| --- | --- | --- | --- |
| Upload and search own documents, chat, read metrics | ✓ | ✓ | ✓ |
| Propose incidents (API, chat, MCP) | | ✓ | ✓ |
| Read incidents and actions | | own | all |
| Approve actions | | | ✓, not their own proposals |
| Reject and execute actions | | | ✓ |
| Read audit log | | own events | all |

Mutations follow **proposal → approval → execution**. An action needs an unexpired
approval from an administrator **other than the proposer** before it can run, so an
administrator's own proposals need a second administrator (they can still reject, i.e.
withdraw, them). Approval and execution lock the action row
and re-read it, so concurrent requests cannot approve and reject the same action or
execute it twice (covered by Postgres integration tests). Rejected and expired actions
never execute. Permission failures return 403, missing or foreign resources 404, and
state conflicts 409.

Authentication uses HTTP-only, `SameSite=Lax` cookies carrying a short-lived JWT; the
role is read from the database on every request, never trusted from the token. With
`APP_ENV=production` the API refuses to start if `JWT_SECRET` is a placeholder or
shorter than 32 characters, or if `AUTH_COOKIE_SECURE` is false.

Redis-backed fixed-window limits cap logins per account (10/min) and per client address
(30/min), and registrations per client address (10/hour). Client addresses are stored
in Redis only as keyed hashes with a TTL. Behind a reverse proxy, set
`FORWARDED_ALLOW_IPS` to the proxy's address so uvicorn uses `X-Forwarded-For`;
otherwise every client shares the proxy's limit. All limits fail open when Redis is
unavailable, which `/ready` reports.

The web app sends a Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff`,
`Referrer-Policy`, and `Permissions-Policy`; API responses send `nosniff`,
`X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer`. The CSP forbids framing
(clickjacking of approval buttons), plugins, `<base>` injection, foreign form targets,
and network requests to anything but the app and the API. `script-src` still allows
inline scripts because Next.js hydration needs them without per-request nonces. HSTS is
left to the TLS-terminating proxy.

`POST /mcp` is an authenticated **MCP-style JSON-RPC adapter**. It supports
`tools/list` and `tools/call` over the same services and RBAC as the API. It is not a
full Model Context Protocol server: there is no `initialize` handshake, capability
negotiation, Streamable HTTP transport, batching, or notifications. Malformed JSON is
rejected with HTTP 422 rather than a JSON-RPC `-32700` error, and tool results use a
non-standard `{"type": "json"}` content item.

## Agent routing

The local agent is deterministic and intentionally conservative:

| Message | Behavior |
| --- | --- |
| “What is the latency?” / “what metrics are available?” | `get_metric` (read-only) |
| “Create an incident and restart the api service” | `create_incident` proposal with `restart_service{service: api}`; approval required |
| “Create an incident because latency is high” | Proposal with **no** action attached; nothing can run |
| “Create an incident and restart the database” | Proposal with no action; the reply explains that only `api`, `worker`, and `web` can be restarted |
| “Can you restart the API?” / “roll back the deployment” | Declined with guidance; no tool runs |
| “Why did we roll back yesterday?” / “don’t create an incident” | Treated as a question; searches documents; no incident is created |
| “Show me incident `<uuid>`” | `get_incident` (owner or admin only) |
| Anything else | Owner-scoped document search, or a short capabilities message |

Only an explicit, non-negated request to create an incident can reach the
proposal-only mutation tool, and actions are attached only for supported actions with an
allowlisted target named in the message. Rollbacks need a deployment identifier, so they
are proposed through the incidents API rather than chat.

## Demo flow

```bash
cp .env.example .env            # then replace the placeholder values
docker compose --env-file .env -f infra/docker/docker-compose.yml up --build -d
```

1. Register three accounts in the web app (every new account is a viewer).
2. Promote two of them and seed demo metrics (labelled `demo-seed`, not live telemetry):

   ```bash
   docker compose --env-file .env -f infra/docker/docker-compose.yml exec api python -m app.manage set-role analyst@example.com analyst
   docker compose --env-file .env -f infra/docker/docker-compose.yml exec api python -m app.manage set-role admin@example.com admin
   docker compose --env-file .env -f infra/docker/docker-compose.yml exec api python -m app.manage seed-demo-metrics
   ```

3. Upload a PDF, TXT, or Markdown document; its status updates as the worker processes it.
4. Ask a question about it and inspect the quoted excerpt and sources.
5. Ask “What is the latency?”.
6. As a viewer, ask to create an incident and see the permission error.
7. As the analyst, ask “Create an incident and restart the api service”.
8. As the administrator, open **Incidents and approvals**, approve, then execute.
9. Inspect `GET /audit-logs` (analysts see only their own events).

## Evaluation and observability

```bash
cd apps/api
python -m app.evaluation
```

The evaluator runs 31 deterministic cases against real code paths in an in-memory
database: document processing and local-embedding retrieval (grounding, off-topic
rejection, owner isolation, extractive quoting), intent routing, agent mutation safety,
the approval lifecycle, and MCP error handling. It is regression evaluation of behavior
contracts, not a measure of language-model answer quality.

Application logs from the API and worker are JSON lines with `timestamp`, `level`,
`logger`, `event` (for example `http.request.completed`, `rag.retrieval.completed`,
`tool.completed`, `incident.executed`), a `request_id` shared by every event in a
request, and fields such as `user_id`, `tool`, `status`, `result_count`, and
`duration_ms`. Keys that look like passwords, tokens, cookies, or secrets are redacted,
and document contents and message text are never logged. A caller-supplied
`X-Request-ID` is reused only if it is short and log-safe. (Alembic migration output
and uvicorn's startup messages remain plain text.)

`/health` checks the API and database (liveness). `/ready` also checks Redis.

## Local development

Docker Compose (recommended):

1. `cp .env.example .env`, then set `POSTGRES_PASSWORD` (also in `DATABASE_URL`) and a
   `JWT_SECRET` of at least 32 random characters.
2. `docker compose --env-file .env -f infra/docker/docker-compose.yml up --build`
3. Open http://localhost:3000 (web), http://localhost:8000/docs (API),
   http://localhost:8000/health and http://localhost:8000/ready.

Postgres and Redis are published on `127.0.0.1` only. Change host ports with `API_PORT`,
`WEB_PORT`, `POSTGRES_PORT`, and `REDIS_PORT`. If you change the API port or host, also
update `NEXT_PUBLIC_API_URL` (inlined into the web bundle at build time, so rebuild the
web image) and `CORS_ORIGINS`.

Running the API and worker on the host, with Postgres and Redis from Compose:

```bash
cd apps/api
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
set -a && source ../../.env && set +a   # export DATABASE_URL, REDIS_URL, JWT_SECRET, ...
alembic upgrade head
uvicorn app.main:app --reload           # API on :8000
python -m app.worker                    # in a second shell
```

## Testing and CI/CD

Backend:

```bash
cd apps/api
pytest                      # SQLite-backed unit and API tests
ruff check app tests && ruff format --check app tests && mypy app
python -m app.evaluation
```

Postgres-only behavior (pgvector search, row locking under concurrency) is covered by
`tests/test_postgres_integration.py`, which is skipped unless `TEST_DATABASE_URL` is set:

```bash
TEST_DATABASE_URL=postgresql+psycopg://opsai:<password>@localhost:5432/opsai pytest tests/test_postgres_integration.py
```

Frontend:

```bash
npm ci
npm run test:web && npm run lint:web && npm run typecheck:web && npm run build:web
```

GitHub Actions (`.github/workflows/ci.yml`) runs four jobs on every push and pull
request: backend checks, tests, and the evaluator; a Postgres job that applies
migrations, runs `alembic check` for model drift, tests a full downgrade and re-upgrade,
and runs the Postgres integration tests; the frontend checks and production build; and
Compose validation against `.env.example`. CI needs no paid AI service or API key.

## Troubleshooting

- **Port already in use**: set `API_PORT`, `WEB_PORT`, `POSTGRES_PORT`, or `REDIS_PORT`
  in `.env`, and keep `NEXT_PUBLIC_API_URL` and `CORS_ORIGINS` consistent.
- **The browser calls the wrong API URL**: `NEXT_PUBLIC_API_URL` is baked in at build
  time; rebuild with `up --build`.
- **Document failed with “Processing did not finish”**: the worker stopped mid-job;
  within about 15 minutes it marks such documents failed instead of leaving them in
  `processing`. Upload the document again. If Redis was unavailable at upload time, the
  document is marked `failed` immediately with a re-upload message.
- **429 “Too many registration attempts”**: registrations are limited to 10 per client
  address per hour. Behind a proxy, check `FORWARDED_ALLOW_IPS`.
- **Signed out after about 30 minutes**: tokens expire after `JWT_EXPIRE_MINUTES`; there
  is no refresh token.
- **The API refuses to start**: with `APP_ENV=production`, the log line names the
  unsafe setting.

## Key engineering decisions

- Deterministic local providers keep development, tests, and CI reproducible.
- Retrieval is owner-scoped and thresholded before any citation is emitted.
- Typed allowlists prevent arbitrary tool, action, target, or SQL selection.
- Mutations are proposals first; execution requires an unexpired approval by a second
  person (separation of duties).
- Row locks plus a fresh re-read make approval and execution safe under concurrency.
- The API, agent, and MCP adapter share one service layer, so RBAC is enforced once.
- The evaluator checks behavior contracts instead of inventing model-quality scores.

## Limitations

- **Local retrieval is lexical, not semantic.** The default embedding is a hashed
  bag of words: it matches exact word forms and cannot match paraphrases or synonyms.
  For example, against a note that says “The VPN is configured with…”, the question
  “How is the VPN configured?” scores 0.48 but “How do I configure the VPN?” scores
  0.22, below the 0.35 threshold. Function words can also push unrelated questions over
  the threshold, so the local provider additionally requires at least one shared content
  word (prefix match) before a chunk counts as grounded. Improving recall (full-text
  ranking in Postgres, or a real embedding model behind the existing provider interface)
  needs a labelled evaluation set first, which the project does not have yet.
- **Local answers are extractive.** They quote the most relevant retrieved sentences
  with their source; they do not summarize or reason across documents. Set
  `CHAT_PROVIDER=openai` and `OPENAI_API_KEY` for generated answers (optional, untested
  in CI).
- **Execution is a persisted boundary.** Approved actions are recorded as executed; no
  external system is called.
- **Lost document jobs are failed, not retried.** Each job atomically claims its
  document, so duplicate deliveries are no-ops. A job lost to a worker crash is detected
  after 15 minutes and its document is marked failed (re-upload to retry), so a file that
  crashes the worker cannot cause a crash loop. Uploads that were never picked up are
  queued again.
- **Sessions are stateless JWTs.** Logout clears the cookie but does not revoke a copied
  token before it expires.
- **The audit log endpoint returns the latest 200 events** with no pagination or
  `created_at` index; fine at current scale, but worth adding before the table grows
  large.
- Deployment concerns such as TLS termination and HSTS, secret management, backups,
  scaling, and metrics or tracing backends are out of scope for this repository.

## Configuration

`.env.example` contains placeholders only; `.env` is git-ignored. All settings are
environment variables (see `apps/api/app/config.py`). Defaults are safe for offline
development, and `APP_ENV=production` turns insecure defaults into a startup failure.
