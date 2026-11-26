"""Data contracts shared by every stage of the pipeline.

Verdict labels are internal enums. The mapping to the organiser's labels happens only in
the output writer (see config.Settings.real_alive_labels / trust_labels).
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_REASONS = 7
MIN_REASONS = 4


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceKind(StrEnum):
    RECORD = "record"
    INDEPENDENT = "independent"
    VENDOR = "vendor"


class Axis(StrEnum):
    REAL_ALIVE = "real_alive"
    TRUST = "trust"


class Polarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class RealAlive(StrEnum):
    YES = "yes"
    UNCLEAR = "unclear"
    NO = "no"


class Trust(StrEnum):
    TRUST = "trust"
    CAUTION = "caution"
    DO_NOT_TRUST = "do_not_trust"


ClaimType = Literal[
    "legal_name",
    "register_number",
    "founded_year",
    "customer",
    "certification",
    "integration",
    "team",
    "other",
]


class Claim(_Model):
    """Something the vendor says about itself. Never evidence in the vendor's favour."""

    id: str
    type: ClaimType
    text: str
    source_url: str
    quote: str


class Fact(_Model):
    """An observation from a record or an independent source."""

    id: str
    collector: str
    statement: str
    axis: Axis
    polarity: Polarity
    weight: float = Field(ge=0.0, le=1.0)
    source_url: str
    quote: str  # "" only for computed facts (verified by resolving source_url)
    source_kind: SourceKind
    fetched_at: datetime
    is_hard_flag: bool = False
    # Machine-readable record to verify against when source_url is a human page that cannot be
    # checked directly (RDAP JSON behind an ICANN lookup link, crt.sh JSON, a CDX query).
    check_url: str = ""
    # What kind of signal this is (e.g. domain_registered, breach, reviews, contradicted_claim).
    topic: str = ""
    # When the observed event happened (registration, first capture, release, breach).
    observed_at: datetime | None = None
    # Low-value material (copyright year, directory listings, site looks): last-resort reasons.
    is_filler: bool = False
    # Facts this one is built on (claims_check), all of which must verify too.
    related_fact_ids: list[str] = Field(default_factory=list)
    # Kind of publisher: record, press, regulator, court, vendor, review, social, forum, blog,
    # directory, other. Decides whether one source is enough for a finding.
    source_type: str = ""
    # The statement was written by our code from the quote or a record (no model check needed).
    code_worded: bool = False
    # Extra markers, e.g. "disclosed" on a breach the vendor announced itself.
    tags: list[str] = Field(default_factory=list)


class ClaimCheck(_Model):
    claim_id: str
    status: Literal["confirmed", "unconfirmed", "contradicted"]
    fact_ids: list[str]
    note: str


class Reason(_Model):
    text: str
    axis: Axis
    polarity: Polarity
    source_url: str
    quote: str
    fact_id: str


class Finding(_Model):
    """A notable contradiction or red flag, built from a verified fact, with its link."""

    text: str
    source_url: str
    fact_id: str


class Verdict(_Model):
    product_url: str
    real_alive: RealAlive
    trust: Trust
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    reasons: list[Reason] = Field(max_length=MAX_REASONS)
    findings: list[Finding]
    run_id: str
    git_sha: str
    model: str
    created_at: datetime
