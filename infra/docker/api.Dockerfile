FROM python:3.12-slim

WORKDIR /app
COPY apps/api/pyproject.toml apps/api/alembic.ini ./
COPY apps/api/app ./app
COPY apps/api/migrations ./migrations

RUN pip install --no-cache-dir .
RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data/documents \
    && chown -R app:app /app /data/documents

USER app
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
