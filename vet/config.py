"""Settings loaded from the environment (.env via python-dotenv). No secrets in code."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from vet import __version__
from vet.schemas import RealAlive, Trust

DEFAULT_JUDGE_MODEL = "claude-opus-5-5"
DEFAULT_FAST_MODEL = "claude-haiku-5-5"
DEFAULT_PRODUCT_BUDGET_S = 180.0
DEFAULT_COLLECTOR_TIMEOUT_S = 45.0
DEFAULT_FETCH_TIMEOUT_S = 15.0
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_HOST_INTERVAL_S = 1.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_MAX_PARALLEL_PRODUCTS = 5

# Minimum seconds between requests per host. Everything not listed gets
# DEFAULT_HOST_INTERVAL_S (1 request/second). Wayback and crt.sh are pinned at 1 s.
DEFAULT_HOST_INTERVALS: dict[str, float] = {
    "web.archive.org": 1.0,
    "crt.sh": 1.0,
    "api.tavily.com": 0.25,
    "api.company-information.service.gov.uk": 0.5,
}


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    anthropic_api_key: SecretStr | None = None
    tavily_api_key: SecretStr | None = None
    companies_house_api_key: SecretStr | None = None
    backup_llm_api_key: SecretStr | None = None

    judge_model: str = DEFAULT_JUDGE_MODEL
    fast_model: str = DEFAULT_FAST_MODEL

    product_budget_s: float = DEFAULT_PRODUCT_BUDGET_S
    collector_timeout_s: float = DEFAULT_COLLECTOR_TIMEOUT_S
    fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    max_parallel_products: int = DEFAULT_MAX_PARALLEL_PRODUCTS

    default_host_interval_s: float = DEFAULT_HOST_INTERVAL_S
    host_intervals: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_HOST_INTERVALS))

    contact: str = ""
    cache_path: Path = Path(".cache") / "fetch.sqlite"
    runs_dir: Path = Path("runs")

    real_alive_labels: dict[RealAlive, str] = Field(
        default_factory=lambda: {v: v.value for v in RealAlive}
    )
    trust_labels: dict[Trust, str] = Field(default_factory=lambda: {v: v.value for v in Trust})

    @property
    def user_agent(self) -> str:
        contact = f"; contact: {self.contact}" if self.contact else ""
        return f"vet/{__version__} (vendor due-diligence research bot; read-only{contact})"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        load_dotenv(dotenv_path=env_file, override=False)
        values: dict[str, object] = {}

        for field, var in (
            ("anthropic_api_key", "ANTHROPIC_API_KEY"),
            ("tavily_api_key", "TAVILY_API_KEY"),
            ("companies_house_api_key", "COMPANIES_HOUSE_API_KEY"),
            ("backup_llm_api_key", "BACKUP_LLM_API_KEY"),
            ("judge_model", "JUDGE_MODEL"),
            ("fast_model", "FAST_MODEL"),
            ("product_budget_s", "VET_PRODUCT_BUDGET_S"),
            ("collector_timeout_s", "VET_COLLECTOR_TIMEOUT_S"),
            ("fetch_timeout_s", "VET_FETCH_TIMEOUT_S"),
            ("contact", "VET_CONTACT"),
            ("cache_path", "VET_CACHE_PATH"),
            ("runs_dir", "VET_RUNS_DIR"),
        ):
            raw = os.environ.get(var, "").strip()
            if raw:
                values[field] = raw

        host_overrides = _json_env("VET_HOST_INTERVALS")
        if host_overrides:
            values["host_intervals"] = {**DEFAULT_HOST_INTERVALS, **host_overrides}
        for field, var in (
            ("real_alive_labels", "VET_REAL_ALIVE_LABELS"),
            ("trust_labels", "VET_TRUST_LABELS"),
        ):
            labels = _json_env(var)
            if labels:
                values[field] = labels

        return cls.model_validate(values)


def _json_env(var: str) -> dict[str, object]:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{var} must be a JSON object")
    return parsed
