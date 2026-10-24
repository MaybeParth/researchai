"""Runtime settings, loaded from environment variables or a local .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    tavily_api_key: str = ""

    lead_model: str = "claude-sonnet-5-5"  # planning, gap review, report writing
    researcher_model: str = "claude-haiku-5-5"  # per-source reading and evidence extraction

    max_rounds: int = 2  # research rounds in total: round 1 breadth + gap follow-ups
    sources_per_query: int = 4
    min_sources_per_question: int = 2  # distinct websites; below this a question is "thin"
    ask_direction: bool = True  # always ask the user to choose a direction before planning

    db_path: str = "researchai.db"

    def missing_keys(self) -> list[str]:
        return [
            name
            for name, value in [
                ("ANTHROPIC_API_KEY", self.anthropic_api_key),
                ("TAVILY_API_KEY", self.tavily_api_key),
            ]
            if not value
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
