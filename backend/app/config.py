from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SBP_")

    database_url: str = "sqlite:///./solar.db"
    # Override in any real deployment: tokens signed with the default are forgeable.
    jwt_secret: str = "dev-only-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 24 * 7
    cors_origins: list[str] = ["http://localhost:4200"]
    # Encrypts stored third-party API keys (e.g. Fox ESS). Override in any real deployment,
    # and keep it stable: changing it makes saved keys unreadable.
    credentials_secret: str = "dev-only-credentials-secret-change-me"
    fox_ess_base_url: str = "https://www.foxesscloud.com"
    # Open-Meteo: reanalysis archive for older days, historical forecasts for the last week
    # (the archive lags real time by several days).
    open_meteo_archive_url: str = "https://archive-api.open-meteo.com/v1/archive"
    open_meteo_recent_url: str = "https://historical-forecast-api.open-meteo.com/v1/forecast"


settings = Settings()
