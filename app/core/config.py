from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables and .env file.
    """
    # Database Settings (PostgreSQL)
    database_url: str

    # JWT Authentication Settings
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    # Environment & Cookie Security
    environment: str = "development"
    cookie_secure: bool = False

    @property
    def is_cookie_secure(self) -> bool:
        """
        Enforces secure cookies in production, while allowing local dev/testing over HTTP.
        """
        if self.environment.lower() == "production":
            return True
        return self.cookie_secure

    # Trusted Proxies (CIDR ranges / hostnames for secure rate limiting)
    trusted_proxies: str = "127.0.0.1,::1,localhost,testclient"

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
    cache_services_ttl: int = 300  # 5 minutes for event discovery listings and details
    cache_seats_ttl: int = 30      # 30 seconds for live seat maps
    services_rate_limit: int = 120 # 120 req/minute per IP for discovery endpoints
    bookings_rate_limit: int = 60  # 60 req/minute per IP for booking holds
    payments_rate_limit: int = 60  # 60 req/minute per IP for payment endpoints
    auth_rate_limit: int = 100     # 100 req/minute per IP for auth/login endpoints
    max_concurrent_auth_hashes: int = 10  # Max concurrent Argon2 hashes server-wide (CPU starvation guard)
    rate_limit_fail_closed: bool = False  # False = fail-open (availability priority), True = fail-closed (security priority)

    # Database Connection Pool Settings
    db_pool_size: int = 25
    db_max_overflow: int = 25
    db_pool_timeout: int = 10

    # Stripe Settings
    STRIPE_SECRET_KEY: str
    STRIPE_PUBLISHABLE_KEY: str
    STRIPE_WEBHOOK_SECRET: str

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# Global settings singleton
settings = Settings()

