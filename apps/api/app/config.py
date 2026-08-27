from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    api_version: str = "0.1.0"
    database_url: str = "postgresql+psycopg://opsai:opsai_local_password@localhost:5432/opsai"
    redis_url: str = "redis://localhost:6379/0"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    jwt_secret: str = "development-only-change-this-secret"
    jwt_expire_minutes: int = 30
    auth_cookie_name: str = "opsai_access_token"
    auth_cookie_secure: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", case_sensitive=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
