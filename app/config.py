from pydantic_settings import SettingsConfigDict, BaseSettings

class Settings(BaseSettings):
    # Database Settings
    database_url: str
    secret_key: str
    algorithm: str
    access_token_expire_minutes: int

    # Email Settings
    email_host_user: str
    email_host_password: str
    default_from_email: str
    email_smtp_server: str = "smtp.gmail.com"
    email_smtp_port: int = 587
    
    # Redis Settings
    redis_url: str = "redis://localhost:6379/0"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()