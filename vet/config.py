"""Settings loaded from the environment (.env via python-dotenv). No secrets in code."""

import json
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from vet import __version__
from vet.schemas import Overall, RealAlive, Trust

DEFAULT_JUDGE_MODEL = "claude-opus-5-5"
DEFAULT_FAST_MODEL = "claude-haiku-5-5"
DEFAULT_PRODUCT_BUDGET_S = 180.0
DEFAULT_COLLECTOR_TIMEOUT_S = 45.0
# The site reader may fall back to Wayback copies, which share a 20 req/min limit.
DEFAULT_SITE_READER_TIMEOUT_S = 75.0
DEFAULT_FETCH_TIMEOUT_S = 15.0
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_HOST_INTERVAL_S = 1.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_MAX_PARALLEL_PRODUCTS = 4  # fewer products sharing the Wayback limit at once
DEFAULT_BREACH_RECENT_YEARS = 3
OutputFormat = Literal["json", "csv", "stackgini"]
OUTPUT_FORMATS: tuple[str, ...] = ("json", "csv", "stackgini")
# Columns of the stackgini verdict file, until the organiser confirms them (VET_STACKGINI_COLUMNS,
# a comma-separated list). "action" is available as an extra column.
DEFAULT_STACKGINI_COLUMNS: tuple[str, ...] = (
    "id",
    "url",
    "product",
    "vendor",
    "overall",
    "green_flags",
    "yellow_flags",
    "red_flags",
    "reasoning",
)

TAVILY_CREDITS_BASIC = 1
TAVILY_CREDITS_ADVANCED = 2


class LabelThresholds(BaseModel):
    """Score -> label cut-offs. Scores are sums of weight x polarity (+1/-1) over the scored,
    verified facts of one question. A score exactly at a threshold gets the more cautious
    label."""

    model_config = ConfigDict(frozen=True)

    real_alive_yes_above: float = 0.0
    real_alive_no_at_or_below: float = -0.5
    trust_above: float = 0.3
    do_not_trust_at_or_below: float = -0.5


class HostPolicy(BaseModel):
    """Per-source politeness. Unset fields fall back to the global settings."""

    model_config = ConfigDict(frozen=True)

    min_interval_s: float
    max_retries: int | None = None
    timeout_s: float | None = None


# Everything not listed gets DEFAULT_HOST_INTERVAL_S (1 request/second) and global retries.
DEFAULT_HOST_POLICIES: dict[str, HostPolicy] = {
    "web.archive.org": HostPolicy(min_interval_s=3.0),  # Wayback CDX: <= 20/min
    "crt.sh": HostPolicy(min_interval_s=15.0, max_retries=0, timeout_s=20.0),  # <= 4/min, 1 try
    "api.company-information.service.gov.uk": HostPolicy(min_interval_s=0.6),  # <= 100/min
    "haveibeenpwned.com": HostPolicy(min_interval_s=2.0),  # 1 per 2 s
    "api.tavily.com": HostPolicy(min_interval_s=0.6),  # <= 100/min across all keys
}

# Never fetched directly. Pages on these domains are found via Tavily and quoted from the Tavily
# result content (register records, review sites and LinkedIn block or forbid bots).
SNIPPET_ONLY_DOMAINS: tuple[str, ...] = (
    "northdata.com",
    "northdata.de",
    "g2.com",
    "capterra.com",
    "capterra.de",
    "capterra.co.uk",
    "trustpilot.com",
    "omr.com",
    "softwareadvice.com",
    "gartner.com",
    "linkedin.com",
)

# Record APIs: called without a robots.txt check (still rate-limited, cached and traced).
API_HOSTS: tuple[str, ...] = (
    "rdap.org",
    "crt.sh",
    "web.archive.org",
    "api.company-information.service.gov.uk",
    "haveibeenpwned.com",
    "api.github.com",
    "pypi.org",
    "registry.npmjs.org",
    "itunes.apple.com",
)


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
    site_reader_timeout_s: float = DEFAULT_SITE_READER_TIMEOUT_S
    fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    max_parallel_products: int = DEFAULT_MAX_PARALLEL_PRODUCTS

    default_host_interval_s: float = DEFAULT_HOST_INTERVAL_S
    host_policies: dict[str, HostPolicy] = Field(
        default_factory=lambda: dict(DEFAULT_HOST_POLICIES)
    )
    blocked_domains: tuple[str, ...] = SNIPPET_ONLY_DOMAINS

    tavily_advanced: bool = False
    # Reuse cached LLM answers for identical inputs (the models take no temperature setting).
    llm_cache: bool = True
    # A breach younger than this (or never disclosed by the vendor) makes trust "caution".
    breach_recent_years: int = DEFAULT_BREACH_RECENT_YEARS
    # Labels are decided in code from these cut-offs (VET_LABEL_THRESHOLDS, JSON object).
    label_thresholds: LabelThresholds = Field(default_factory=LabelThresholds)

    # Verdict file format for the organiser ("json", "csv" or "stackgini").
    output_format: OutputFormat = "json"
    # Labelled-set loader: our field name -> column/key name in the organiser's file.
    label_columns: dict[str, str] = Field(
        default_factory=lambda: {
            "url": "url",
            "real_alive": "real_alive",
            "trust": "trust",
            "reasons": "reasons",
            "note": "note",
            "overall": "overall",
            "id": "id",
        }
    )
    stackgini_columns: list[str] = Field(default_factory=lambda: list(DEFAULT_STACKGINI_COLUMNS))
    overall_labels: dict[Overall, str] = Field(
        default_factory=lambda: {v: v.value.capitalize() for v in Overall}
    )

    contact: str = ""
    cache_path: Path = Path(".cache") / "fetch.sqlite"
    runs_dir: Path = Path("runs")

    real_alive_labels: dict[RealAlive, str] = Field(
        default_factory=lambda: {v: v.value for v in RealAlive}
    )
    trust_labels: dict[Trust, str] = Field(default_factory=lambda: {v: v.value for v in Trust})

    @property
    def tavily_search_depth(self) -> str:
        return "advanced" if self.tavily_advanced else "basic"

    @property
    def tavily_credits_per_search(self) -> int:
        return TAVILY_CREDITS_ADVANCED if self.tavily_advanced else TAVILY_CREDITS_BASIC

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
            ("site_reader_timeout_s", "VET_SITE_READER_TIMEOUT_S"),
            ("fetch_timeout_s", "VET_FETCH_TIMEOUT_S"),
            ("contact", "VET_CONTACT"),
            ("cache_path", "VET_CACHE_PATH"),
            ("runs_dir", "VET_RUNS_DIR"),
            ("tavily_advanced", "VET_TAVILY_ADVANCED"),
            ("breach_recent_years", "VET_BREACH_RECENT_YEARS"),
            ("output_format", "VET_OUTPUT_FORMAT"),
            ("llm_cache", "VET_LLM_CACHE"),
        ):
            raw = os.environ.get(var, "").strip()
            if raw:
                values[field] = raw.lower() if field == "output_format" else raw

        columns = os.environ.get("VET_STACKGINI_COLUMNS", "").strip()
        if columns:
            values["stackgini_columns"] = [c.strip() for c in columns.split(",") if c.strip()]
        thresholds = _json_env("VET_LABEL_THRESHOLDS")
        if thresholds:
            values["label_thresholds"] = thresholds
        host_overrides = _json_env("VET_HOST_POLICIES")
        if host_overrides:
            values["host_policies"] = {**DEFAULT_HOST_POLICIES, **host_overrides}
        for field, var in (
            ("real_alive_labels", "VET_REAL_ALIVE_LABELS"),
            ("trust_labels", "VET_TRUST_LABELS"),
            ("label_columns", "VET_LABEL_COLUMNS"),
        ):
            labels = _json_env(var)
            if labels and field == "label_columns":  # partial override keeps the defaults
                labels = {
                    **Settings.model_fields[field].get_default(call_default_factory=True),
                    **labels,
                }
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
