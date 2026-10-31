from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SBP_")

    database_url: str = "sqlite:///./solar.db"
    # Override in any real deployment: tokens signed with the default are forgeable.
    jwt_secret: str = "dev-only-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 24 * 7
    cors_origins: list[str] = ["http://localhost:4200"]


settings = Settings()
