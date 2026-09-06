from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    api_version: str = "0.1.0"
    database_url: str = (
        "postgresql+psycopg://opsai:replace-with-local-database-password@localhost:5432/opsai"
    )
    redis_url: str = "redis://localhost:6379/0"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
