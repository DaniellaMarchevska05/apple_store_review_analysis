from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_api_key: SecretStr | None = None
    llm_model: str = "gpt-5.6-terra"
    llm_api: Literal["responses", "chat"] = "responses"
    llm_base_url: str = "https://api.openai.com/v1"
    database_path: Path = Path("data/reviews.sqlite3")
    collection_timeout_seconds: float = Field(default=90, gt=0, le=600)
    analysis_timeout_seconds: float = Field(default=180, gt=0, le=600)
