# System-counted cartridge usage

## Meaning and limits

The enhanced collector tracks device-counter deltas per black/cyan/magenta/yellow cartridge.
Black uses **total pages**; CMY each use **full-colour pages**. These are pages observed
while a cartridge is tracked, not measured toner consumption or guaranteed cartridge yield.
The first observation starts at zero; it does not reconstruct earlier printing or establish
the actual physical installation date. No record is started without a valid counter basis.
Drums and other supply types are not counted by this feature.

A manual cartridge reset restarts just the selected colour. A changed explicit chip/serial
identity also restarts its record; a first identity attaches without discarding previous
usage. Unknown provenance and model/part/generation fallback identifiers cannot prove
replacement. Devices without genuine identities require manual reset for replacement.

Missing/rejected counters leave usage unchanged. Counter rollbacks preserve already counted
pages and rebase the anchor, without marking the rollback as use. Last use means the UTC
date of the last observed counter increase, not a precise print timestamp. Malformed,
negative, fractional, boolean and nonfinite input counters are ignored.

## Storage and API

Records are additive JSON under `printer_counters.cartridge_id_state.supply_usage`; no
schema or startup migration, derived-run activation, new SNMP OID or external request is
introduced. Existing cartridge metadata remains intact. Single and batch readers load the
same records; the PrevStore cache can be cleared without losing them.

The printer API enriches each toner with `supply_pages`, `first_install_date` and
`last_use_date`. Untracked values are null. `first_install_date` retains the patch's API
name for compatibility but means **tracking start/reset date**. The Persian dashboard
labels and tooltip explicitly explain this distinction and the device-counter approximation;
date strings are HTML-escaped and invalid page values render as placeholders.

Updates to the usage map and manual-reset usage sub-block share an in-process striped lock
and reload the latest stored map rather than a stale earlier poll snapshot. This is **not**
a transaction around the entire poll/reset pipeline and is not cross-process coordination.
Concurrent reset versus an already-running poll is not guaranteed to have an exact physical
replacement boundary. The legacy/experimental collector paths do not gain usage tracking.
Database writes retain existing application's logging/error-handling behaviour; this is not
an independently durable event ledger.

## Independently executed validation

Base: `d39eab29b19bfc83b9d897a4b5e6e2abdff311bd`. Both full-suite runs used the
same Python 3.11 environment with `DB_BACKEND=sqlite PG_DSN=`; baseline was extracted
with `git archive` outside the working checkout. Optional `pgserver==0.1.4` was installed;
local proof tests own disposable PostgreSQL clusters and do not use a deployment DSN.

| Run | Result |
| --- | --- |
| Unmodified base, full pytest suite | 1235 passed, 23 failed, 83 skipped; 539 subtests passed |
| Final feature, full pytest suite | 1262 passed, 22 failed, 83 skipped; 560 subtests passed |
| Focused usage/PG/golden/OID/exporter/logging regression selection | 131 passed; 23 subtests passed |
| Browser DOM tests (`npm test`) | 15 passed, including 3 new supply renderer tests |
| Test split guard | 1084 unittest-style tests assigned exactly once |
| `git diff --check` | passed |

The complete suite is **not green** locally. Failure-ID comparison found no added failures;
the base's stale OID-document failure is resolved. The remaining 22 failure reports
(18 FAILED plus 4 SUBFAILED) are the same as on the base: pg_restore/tool availability,
cutover timestamp expectation, stale JSONB migration/schema expectations, and migration
fixtures requiring an unavailable operational PostgreSQL at port 5432 or missing SQLite
logs tables. These pre-existing failures are not silently excluded or presented as successes.

Usage regressions include invalid counters/stored anchors, fallback identity provenance,
first/changing chip identity, repeated counter resets, all colours and monochrome-only
printing, stale usage snapshots, manual-reset API, legacy JSON compatibility, single/batch
SQLite and PostgreSQL persistence, and actual collector rejection of a transient zero.
DOM regressions execute the production `buildPrinterDetail` function with jsdom and check
counts, tracking labels, missing values, and inert malicious date strings.

OID matrix and exporter source references were regenerated together. The runtime exporter
diff changes only help-text source line references, not metric OIDs or collection behaviour.
No merge, deployment, server migration or maintenance activation is part of this change.
