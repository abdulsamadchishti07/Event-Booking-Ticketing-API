from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables and .env file.
    """
    # Database Settings (PostgreSQL)
    database_url: str

    # JWT Authentication Settings
    secret_key: str
    algorithm: str
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    cookie_secure: bool = False

    # Reservation hold duration
    HOLD_DURATION_MINUTES: int = 10
    
    # Email Settings (Gmail SMTP)
    email_host_user: str = ""
    email_host_password: str = ""
    default_from_email: str = ""
    email_smtp_server: str = "smtp.gmail.com"
    email_smtp_port: int = 587

    # Redis Settings (Caching & Rate Limiting)
    redis_url: str = "redis://localhost:6379/0"

    # Stripe Settings
    STRIPE_SECRET_KEY: str
    STRIPE_PUBLISHABLE_KEY: str
    STRIPE_WEBHOOK_SECRET: str

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# Global settings singleton
settings = Settings()

