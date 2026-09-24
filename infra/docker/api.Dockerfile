FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY apps/api/pyproject.toml apps/api/alembic.ini ./
COPY apps/api/app ./app
COPY apps/api/migrations ./migrations

RUN pip install .
RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data/documents \
    && chown -R app:app /app /data/documents

USER app
EXPOSE 8000
# exec hands PID 1 to uvicorn so SIGTERM triggers a graceful shutdown. Request logs come
# from the API's structured middleware, so uvicorn's access log is disabled.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log"]
