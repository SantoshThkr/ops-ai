# OpsAI architecture

```mermaid
flowchart LR
    Browser[Browser] --> Web[Next.js web]
    Web --> API[FastAPI API]
    API --> Postgres[(PostgreSQL + pgvector)]
    API --> Redis[(Redis)]
    API --> Storage[(Local document storage)]
    Redis --> Worker[Document worker]
    Worker --> Postgres
    Worker --> Storage
```

The web app owns presentation and browser configuration. The API owns validation,
business logic, and persistence access. PostgreSQL is the durable system of record;
Redis carries document jobs to the worker, and local storage holds generated-key
uploads outside the source tree. Shared TypeScript contracts remain intentionally
small until a cross-application use case exists.
