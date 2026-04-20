from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    # Zalo
    ZALO_BOT_TOKEN: str
    ADMIN_ZALO_ID: str = ""  # Zalo ID của admin để nhận thông báo đơn hàng

    # AI / LLM
    OPENAI_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    LLM_PROVIDER: str = ""  # "openai" | "google"

    # Database
    DATABASE_URL: str  # postgresql+asyncpg://user:pass@host/db

    # Google Sheets
    GOOGLE_SHEET_ID: str = ""
    GOOGLE_CREDENTIALS_FILE: str = "credentials.json"
    # Nội dung credentials.json dạng chuỗi JSON (dùng trên Railway/cloud thay cho file)
    GOOGLE_CREDENTIALS_JSON: str = ""

    # PayOS
    PAYOS_CLIENT_ID: str = ""
    PAYOS_API_KEY: str = ""
    PAYOS_CHECKSUM_KEY: str = ""
    PAYOS_CANCEL_URL: str = ""
    PAYOS_RETURN_URL: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
