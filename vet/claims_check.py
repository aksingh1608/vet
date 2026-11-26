"""Match the vendor's claims against the facts.

Founding year (AGENTS.md decision 2):
* A company register that disagrees with the claimed year by more than
  REGISTER_TOLERANCE_YEARS -> ``contradicted`` and a hard-flag NEGATIVE trust fact.
* No register, and the claimed year is more than FOUNDING_GAP_YEARS before ALL the age evidence
  (domain registration, first certificate, earliest archive capture; at least two of them) ->
  ``unconfirmed`` plus a NEGATIVE medium-weight fact. Domain records cannot contradict a
  founding year: many companies are older than their website.

Register number / legal name: ``contradicted`` when legal_id found a mismatch, ``confirmed`` when
it found the register record. Customers: ``confirmed`` only by an independent mention.
Everything else stays ``unconfirmed``.

Facts made here quote the vendor page that makes the claim; the record facts they rest on are
listed in ``related_fact_ids`` and must verify too before the fact can become a reason.
"""

import re
from datetime import datetime

from vet.schemas import Axis, Claim, ClaimCheck, Fact, Polarity, SourceKind

NAME = "claims_check"
FOUNDING_GAP_YEARS = 3
REGISTER_TOLERANCE_YEARS = 2
MIN_AGE_SOURCES = 2
AGE_TOPICS = {
    "domain_registered": "the domain was registered on {date}",
    "first_capture": "the earliest Wayback Machine capture is from {date}",
    "first_certificate": "the first logged certificate dates from {date}",
}
FOUNDING_GAP_WEIGHT = 0.5
CONTRADICTION_WEIGHT = 0.7
_YEAR = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
_NAME_TOKEN = re.compile(r"\b[A-Z][\w&.-]{2,}")


def claimed_year(claim: Claim) -> int | None:
    m = _YEAR.search(claim.quote) or _YEAR.search(claim.text)
    return int(m.group(1)) if m else None


def _when(fact: Fact) -> datetime:
    if fact.observed_at is None:  # callers filter on observed_at
        raise ValueError(f"fact {fact.id} has no observed_at")
    return fact.observed_at


def _fact(
    n: int, claim: Claim, statement: str, related: list[Fact], today: datetime, *, hard: bool
) -> Fact:
    return Fact(
        id=f"{NAME}-{n}",
        collector=NAME,
        statement=statement,
        axis=Axis.TRUST,
        polarity=Polarity.NEGATIVE,
        weight=CONTRADICTION_WEIGHT if hard else FOUNDING_GAP_WEIGHT,
        source_url=claim.source_url,
        quote=claim.quote,
        source_kind=SourceKind.VENDOR,
        fetched_at=today,
        is_hard_flag=hard,
        topic="contradicted_claim" if hard else "founding_year_gap",
        related_fact_ids=[f.id for f in related],
    )


def check_founding(
    claim: Claim, facts: list[Fact], domain: str, n: int, today: datetime
) -> tuple[ClaimCheck, Fact | None]:
    year = claimed_year(claim)
    if year is None:
        return ClaimCheck(
            claim_id=claim.id, status="unconfirmed", fact_ids=[], note="no year"
        ), None

    registers = [f for f in facts if f.topic == "register_founded" and f.observed_at]
    if registers:
        reg = min(registers, key=_when)
        reg_year = _when(reg).year
        if abs(year - reg_year) <= REGISTER_TOLERANCE_YEARS:
            return (
                ClaimCheck(
                    claim_id=claim.id,
                    status="confirmed",
                    fact_ids=[reg.id],
                    note=f"register shows {reg_year}",
                ),
                None,
            )
        statement = (
            f"The site says the company was founded in {year}; the company register shows "
            f"it was registered in {reg_year}"
        )
        fact = _fact(n, claim, statement, [reg], today, hard=True)
        check = ClaimCheck(
            claim_id=claim.id, status="contradicted", fact_ids=[reg.id, fact.id], note=statement
        )
        return check, fact

    age = [f for f in facts if f.topic in AGE_TOPICS and f.observed_at]
    if len(age) < MIN_AGE_SOURCES:
        return (
            ClaimCheck(
                claim_id=claim.id,
                status="unconfirmed",
                fact_ids=[f.id for f in age],
                note="not enough age records to compare",
            ),
            None,
        )
    earliest = min(age, key=_when)
    earliest_year = _when(earliest).year
    if year < earliest_year - FOUNDING_GAP_YEARS:
        detail = AGE_TOPICS[earliest.topic].format(date=_when(earliest).date().isoformat())
        statement = f"The site says the company was founded in {year}; {detail} ({domain})"
        fact = _fact(n, claim, statement, [earliest], today, hard=False)
        check = ClaimCheck(
            claim_id=claim.id,
            status="unconfirmed",
            fact_ids=[f.id for f in age] + [fact.id],
            note=f"claimed {year}, all {len(age)} age records start in {earliest_year} or later",
        )
        return check, fact
    return (
        ClaimCheck(
            claim_id=claim.id,
            status="unconfirmed",
            fact_ids=[f.id for f in age],
            note=f"consistent with age records (earliest {earliest_year})",
        ),
        None,
    )


def check_register(claim: Claim, facts: list[Fact]) -> ClaimCheck:
    legal = [f for f in facts if f.collector == "legal_id"]
    bad = [f for f in legal if f.topic == "contradicted_claim"]
    if bad:
        return ClaimCheck(
            claim_id=claim.id,
            status="contradicted",
            fact_ids=[f.id for f in bad],
            note=bad[0].statement,
        )
    good = [f for f in legal if f.topic == "register_record" and f.polarity == Polarity.POSITIVE]
    if good:
        return ClaimCheck(
            claim_id=claim.id,
            status="confirmed",
            fact_ids=[f.id for f in good],
            note="register record",
        )
    return ClaimCheck(
        claim_id=claim.id, status="unconfirmed", fact_ids=[], note="no register record"
    )


def check_customer(claim: Claim, facts: list[Fact], vendor_names: set[str]) -> ClaimCheck:
    names = {
        t.strip(".").lower()
        for t in _NAME_TOKEN.findall(claim.text + " " + claim.quote)
        if t.lower() not in vendor_names and t.lower() not in {"vendor", "the", "our"}
    }
    support = [
        f
        for f in facts
        if f.source_kind == SourceKind.INDEPENDENT
        and f.polarity != Polarity.NEGATIVE
        and any(n and n in f"{f.statement} {f.quote}".lower() for n in names)
    ]
    if support:
        return ClaimCheck(
            claim_id=claim.id,
            status="confirmed",
            fact_ids=[f.id for f in support],
            note="mentioned by an independent source",
        )
    return ClaimCheck(
        claim_id=claim.id, status="unconfirmed", fact_ids=[], note="no independent mention found"
    )


def check_claims(
    claims: list[Claim],
    facts: list[Fact],
    domain: str,
    vendor_names: set[str],
    today: datetime,
) -> tuple[list[ClaimCheck], list[Fact]]:
    checks: list[ClaimCheck] = []
    new: list[Fact] = []
    founding_done = False
    for claim in claims:
        if claim.type == "founded_year":
            if founding_done:  # one founding-year fact per product is enough
                checks.append(
                    ClaimCheck(
                        claim_id=claim.id, status="unconfirmed", fact_ids=[], note="duplicate"
                    )
                )
                continue
            check, fact = check_founding(claim, facts, domain, len(new), today)
            founding_done = True
            checks.append(check)
            if fact is not None:
                new.append(fact)
        elif claim.type in ("register_number", "legal_name"):
            checks.append(check_register(claim, facts))
        elif claim.type == "customer":
            checks.append(check_customer(claim, facts, {n.lower() for n in vendor_names if n}))
        else:
            checks.append(
                ClaimCheck(
                    claim_id=claim.id,
                    status="unconfirmed",
                    fact_ids=[],
                    note="no independent record checked for this claim type",
                )
            )
    return checks, new
