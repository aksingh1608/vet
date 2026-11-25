# Norma findings log

Every finding from Norma `live_check`, one row each. Status is **fixed** or **accepted** (with why).

| Date | Branch | Rule | File | Status | Why / fix |
|---|---|---|---|---|---|
| 2026-11-25 | feat/phase-1-skeleton | — (check not run) | all Phase 1 files | accepted | Norma `live_check` returned `organisation_unresolved` for this connection, so no automated check ran. Code was self-reviewed against python-norma-ruleset (63 rules) and secrets-norma-ruleset (13 rules). Re-run `live_check` before merging. |
| 2026-11-25 | feat/phase-1-skeleton | py-mng-ip-literal-1.0 (self-review, possible) | vet/config.py | accepted | `DEFAULT_HOST_INTERVALS` names API hosts (web.archive.org, crt.sh, …) as rate-limit defaults. They are overridable via `VET_HOST_INTERVALS`; the hosts are part of the spec, not deployment config. |
