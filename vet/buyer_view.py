"""Buyer view: one traffic light per product (Green, Yellow, Red) with flags and a reasoning.

This module holds the BASELINE mapping from the two-axis verdict and the pieces every buyer
view shares (actions, flag wording and order, the failed-run fallback). A categorised view
built from verified facts may replace the label later; the baseline stays as its fallback.
"""

import logging

from vet.schemas import (
    BuyerVerdict,
    Fact,
    Flag,
    FlagColour,
    Overall,
    Polarity,
    RealAlive,
    Trust,
    Verdict,
)

logger = logging.getLogger(__name__)

ACTIONS: dict[Overall, str] = {
    Overall.RED: "avoid for this use",
    Overall.YELLOW: "hold for one named check",
    Overall.GREEN: "shortlist",
}
NO_FLAGS = "None identified."
FLAG_SEPARATOR = " | "
# Source strength for flag order: records first, vendor statements last.
SOURCE_RANK: dict[str, int] = {
    "regulator": 0,
    "court": 0,
    "record": 1,
    "register": 1,
    "press": 2,
    "independent": 2,
    "review": 3,
    "directory": 4,
    "forum": 4,
    "social": 4,
    "blog": 4,
    "other": 5,
    "vendor": 6,
}
UNKNOWN_SOURCE_RANK = 5


def source_type_of(fact: Fact | None) -> str:
    if fact is None:
        return "source"
    return fact.source_type or fact.source_kind.value


def flag_sort_key(flag: Flag) -> tuple[int, int]:
    return (0 if flag.decisive else 1, SOURCE_RANK.get(flag.source_type, UNKNOWN_SOURCE_RANK))


def order_flags(flags: list[Flag]) -> list[Flag]:
    """The deciding flag first, then by source strength. Duplicate facts appear once."""
    seen: set[tuple[str, str]] = set()
    out: list[Flag] = []
    for flag in sorted(flags, key=flag_sort_key):
        key = (flag.colour.value, flag.fact_id or flag.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(flag)
    return out


def flag_cell(flags: list[Flag], colour: FlagColour) -> str:
    chosen = [f for f in flags if f.colour == colour]
    if not chosen:
        return NO_FLAGS
    return FLAG_SEPARATOR.join(render_flag(f) for f in chosen)


def render_flag(flag: Flag) -> str:
    text = flag.text.strip().rstrip(".").replace(FLAG_SEPARATOR, "; ")
    return f"{text}. ({flag.source_type or 'source'}: {flag.source_url})"


def baseline_overall(verdict: Verdict) -> Overall:
    if verdict.real_alive == RealAlive.NO or verdict.trust == Trust.DO_NOT_TRUST:
        return Overall.RED
    if verdict.real_alive == RealAlive.YES and verdict.trust == Trust.TRUST:
        return Overall.GREEN
    return Overall.YELLOW


def baseline_flags(verdict: Verdict, facts: list[Fact], overall: Overall) -> list[Flag]:
    by_id = {f.id: f for f in facts}
    flags: list[Flag] = []
    for reason in verdict.reasons:
        if reason.polarity == Polarity.NEUTRAL:
            continue
        colour = FlagColour.GREEN if reason.polarity == Polarity.POSITIVE else FlagColour.YELLOW
        flags.append(
            Flag(
                text=reason.text,
                colour=colour,
                source_url=reason.source_url,
                source_type=source_type_of(by_id.get(reason.fact_id)),
                fact_id=reason.fact_id,
            )
        )
    finding_colour = FlagColour.RED if overall == Overall.RED else FlagColour.YELLOW
    for finding in verdict.findings:
        flags.append(
            Flag(
                text=finding.text,
                colour=finding_colour,
                source_url=finding.source_url,
                source_type=source_type_of(by_id.get(finding.fact_id)),
                fact_id=finding.fact_id,
                decisive=overall == Overall.RED,
            )
        )
    return order_flags(flags)


def not_checked_clause(not_checked: list[str]) -> str:
    return f" Not checked: {'; '.join(not_checked)}." if not_checked else ""


def baseline_view(
    verdict: Verdict,
    facts: list[Fact],
    not_checked: list[str],
    *,
    input_id: str = "",
    product: str = "",
    vendor: str = "",
    url: str = "",
) -> BuyerVerdict:
    overall = baseline_overall(verdict)
    reasoning = (
        f"{overall.value.capitalize()}: {verdict.summary.strip()} "
        f"Buyer action: {ACTIONS[overall]}.{not_checked_clause(not_checked)}"
    )
    return BuyerVerdict(
        input_id=input_id,
        url=url or verdict.product_url,
        product=product,
        vendor=vendor,
        overall=overall,
        action=ACTIONS[overall],
        flags=baseline_flags(verdict, facts, overall),
        reasoning=reasoning,
        method="baseline",
        not_checked=not_checked,
    )


def failed_view(
    url: str, error: str, *, input_id: str = "", product: str = "", vendor: str = ""
) -> BuyerVerdict:
    """The row for a product whose run raised: Yellow, nothing checked, says why."""
    return BuyerVerdict(
        input_id=input_id,
        url=url,
        product=product,
        vendor=vendor,
        overall=Overall.YELLOW,
        action=ACTIONS[Overall.YELLOW],
        categories=["not_enough_evidence"],
        reasoning=(
            f"Yellow: run failed: {error[:160]}. The automated review stopped before any "
            "evidence could be weighed, so no call can be made either way. Buyer action: rerun "
            "the review, then check ownership, incidents and regulator records by hand. "
            "Not checked: all sources (the run failed)."
        ),
        method="failed",
        not_checked=["all sources"],
    )
