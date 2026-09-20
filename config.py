from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    # LLM API Keys
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""

    # GitHub
    GITHUB_TOKEN: str = ""

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/codereview"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Agent configuration
    MAX_PARALLEL_AGENTS: int = 5
    AGENT_TIMEOUT_SECONDS: int = 30

    # Feature: PR comment write-back
    ENABLE_PR_COMMENT: bool = False          # top-level PR comment
    ENABLE_INLINE_COMMENT: bool = False      # per-finding inline review

    # Feature: dedup cache (skip re-running agents if same commit SHA)
    ENABLE_DEDUP_CACHE: bool = True
    DEDUP_CACHE_TTL: int = 86_400            # seconds, default 24 h
    REVIEW_RULESET_VERSION: str = "1"       # Bump for external analysis configuration changes.

    # Feature: GitHub Webhook
    GITHUB_WEBHOOK_SECRET: str = ""

    # Feature: Slack / 企业微信 notification
    ENABLE_NOTIFY: bool = False
    SLACK_WEBHOOK_URL: str = ""             # Slack Incoming Webhook URL
    WECHAT_WEBHOOK_URL: str = ""            # 企业微信机器人 Webhook URL
    NOTIFY_ON_SEVERITIES: str = "CRITICAL,HIGH"  # comma-separated


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
