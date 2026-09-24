from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_SECRET_PREFIXES = ("replace-with", "changeme", "change-me")
MIN_JWT_SECRET_LENGTH = 32


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


def insecure_settings(settings: Settings) -> list[str]:
    """Describe settings that are acceptable for local development but not for deployment."""
    problems: list[str] = []
    secret = settings.jwt_secret.strip()
    if secret.lower().startswith(_PLACEHOLDER_SECRET_PREFIXES):
        problems.append("JWT_SECRET is still a placeholder value")
    elif len(secret) < MIN_JWT_SECRET_LENGTH:
        problems.append(f"JWT_SECRET must be at least {MIN_JWT_SECRET_LENGTH} characters")
    if not settings.auth_cookie_secure:
        problems.append("AUTH_COOKIE_SECURE must be true when served over HTTPS")
    if "*" in settings.cors_origin_list:
        problems.append("CORS_ORIGINS cannot contain '*' with credentialed requests")
    return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
