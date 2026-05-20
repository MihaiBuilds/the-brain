"""
Configuration — loads from environment variables with sensible defaults.

All settings in one place. No hardcoded paths. Docker and local both work.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # Database
    db_host: str = os.getenv("DB_HOST", "localhost")
    db_port: int = int(os.getenv("DB_PORT", "5432"))
    db_name: str = os.getenv("DB_NAME", "the_brain")
    db_user: str = os.getenv("DB_USER", "the_brain")
    db_password: str = os.getenv("DB_PASSWORD", "the_brain")

    # Memory Vault — The Brain talks to it over its REST API.
    memory_vault_url: str = os.getenv("MEMORY_VAULT_URL", "http://localhost:8000")
    memory_vault_token: str = os.getenv("MEMORY_VAULT_TOKEN", "")

    # Local LLM — OpenAI-compatible endpoint (LM Studio).
    llm_base_url: str = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
    llm_api_key: str = os.getenv("LLM_API_KEY", "lm-studio")
    llm_model: str = os.getenv("LLM_MODEL", "")

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
