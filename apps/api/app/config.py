from functools import lru_cache
from hashlib import sha256
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_PLACEHOLDER_SECRET_PREFIXES = ("replace-with", "changeme", "change-me")
MIN_JWT_SECRET_LENGTH = 32
# SHA-256 of credentials that were once committed to this repository's .env.example.
# They are public, so production must never run with them.
_PUBLISHED_CREDENTIAL_HASHES = frozenset(
    {
        "c3b4fdccb06ea3a68e9f16385ede15f84cb605e14e4d7d24ba20c823c7da4be7",
        "14e389037b2d4e8e68a29ba82ce7900a3e9498502e35b182b7130d78c67d2847",
    }
)


class Settings(BaseSettings):
    app_env: Literal["development", "test", "production"] = "development"
    api_version: str = "0.1.0"
    database_url: str = (
        "postgresql+psycopg://opsai:replace-with-local-database-password@localhost:5432/opsai"
    )
    redis_url: str = "redis://localhost:6379/0"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    log_level: str = "INFO"
    jwt_secret: str = "replace-with-development-secret-value"
    jwt_expire_minutes: int = 30
    auth_cookie_name: str = "opsai_access_token"
    auth_cookie_secure: bool = False
    storage_dir: str = "storage"
    max_file_size: int = 10 * 1024 * 1024
    # Hard cap on parsing one upload; must stay well below the 15-minute stale-job window.
    document_processing_time_limit_seconds: float = 120.0
    chunk_size: int = 1000
    chunk_overlap: int = 100
    embedding_provider: str = "local"
    embedding_model: str = "local-hash"
    embedding_api_key: str | None = None
    embedding_api_url: str | None = None
    embedding_dimension: int = 384
    retrieval_top_k: int = 5
    retrieval_similarity_threshold: float = 0.35
    queue_name: str = "opsai:document-processing"
    chat_provider: str = "local"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    provider_timeout_seconds: float = 30.0

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", case_sensitive=False)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


def _is_published(value: str) -> bool:
    return sha256(value.encode()).hexdigest() in _PUBLISHED_CREDENTIAL_HASHES


def _database_password(database_url: str) -> str:
    try:
        return make_url(database_url).password or ""
    except ArgumentError:
        return ""


def insecure_settings(settings: Settings) -> list[str]:
    """Describe settings that are acceptable for local development but not for deployment."""
    problems: list[str] = []
    secret = settings.jwt_secret.strip()
    if secret.lower().startswith(_PLACEHOLDER_SECRET_PREFIXES):
        problems.append("JWT_SECRET is still a placeholder value")
    elif _is_published(secret):
        problems.append("JWT_SECRET was published in this repository's history; rotate it")
    elif len(secret) < MIN_JWT_SECRET_LENGTH:
        problems.append(f"JWT_SECRET must be at least {MIN_JWT_SECRET_LENGTH} characters")
    database_password = _database_password(settings.database_url)
    if database_password.lower().startswith(_PLACEHOLDER_SECRET_PREFIXES) or _is_published(
        database_password
    ):
        problems.append("DATABASE_URL uses a placeholder or published password; rotate it")
    if not settings.auth_cookie_secure:
        problems.append("AUTH_COOKIE_SECURE must be true when served over HTTPS")
    if "*" in settings.cors_origin_list:
        problems.append("CORS_ORIGINS cannot contain '*' with credentialed requests")
    return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
