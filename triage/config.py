"""Runtime configuration, sourced from environment (12-factor)."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DIAG_", env_file=".env", extra="ignore")

    # --- Elasticsearch ---
    es_hosts: list[str] = Field(default=["http://localhost:9200"])
    es_api_key: str | None = None
    es_log_index: str = "diag-logs"
    es_incident_index: str = "diag-incidents"
    es_bulk_size: int = 500
    es_ilm_hot_days: int = 7
    es_ilm_delete_days: int = 30

    # --- LLM agent ---
    anthropic_api_key: str | None = None
    model: str = "claude-opus-4-7"
    agent_max_tokens: int = 4096
    agent_max_tool_turns: int = 6
    # Disable real API calls in CI/tests; the agent returns a deterministic stub.
    llm_offline: bool = False

    # --- MCP ---
    mcp_transport: str = "stdio"  # "stdio" | "sse"
    mcp_sse_url: str = "http://localhost:8765/sse"

    # --- Detection / clustering ---
    detect_window_s: int = 300          # incident correlation window
    rate_spike_zscore: float = 3.0      # anomaly threshold on per-template rate
    min_cluster_size: int = 2           # failures needed to open an incident

    # --- Routing ---
    slack_webhook_url: str | None = None
    pagerduty_routing_key: str | None = None
    alert_throttle_s: int = 900         # suppress duplicate alerts within window

    # --- Service ---
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"


@lru_cache
def get_settings() -> Settings:
    return Settings()
