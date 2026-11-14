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

## Parallel consumption information (after PR19)

The page-count formula, counter bases, records, identity/reset handling and yield engine
are unchanged. The added information button is read-only and appears only beside CMYK
supplies on devices explicitly identified as `device_type=color`. Drums, sensors and mono
or unknown devices do not get this button. It reuses the application's modal styling;
a lazily created native dialog supplies focus containment, with Escape, close-button and
backdrop dismissal, and restores focus to a still-connected opener. No UI library, extra
polling, SNMP call, API request, database migration or consumption history is added.

`usage` remains the authoritative, unrounded integer reported by the collector. Toshiba's
two collection paths now return `usage_m = usage / 1_000_000` without decimal quantization.
The existing `usage_m` API field remains a number for compatibility; it is a derived
floating-point convenience, not an arbitrary-precision replacement for `usage`. Backend
calculations must use raw `usage`, not the display unit. The UI derives Mega Dots from the
raw reading in preference to a legacy rounded `usage_m` and rounds to two decimals **only
for display**. A reported zero is distinct from missing data. Unsafe JavaScript integers
are not displayed as exact dot counts (the Python/API raw integer is retained).

The popup shows per-colour page-count basis, observed device counters, raw consumption,
Mega Dots, tracking dates and sample timestamp, with explicit limitations. It does not
invent a consumption baseline: "consumption since tracking began" is unavailable because
no independent consumption baseline is stored. Pages using a particular colour are not
inferred from an interval's dot delta. The actual unit/reset lifecycle of vendor telemetry
still needs model-specific validation. Snapshot values do not live-refresh inside an open
dialog. This parallel inspection path never automatically becomes an authoritative input.

The asset version default is bumped; deployments with an explicit `ASSET_VERSION` override
must update that override or otherwise invalidate cached dashboard assets when deploying.
The new helper loads before the dashboard renderer. All dynamic popup content uses DOM
`textContent`; button snapshot attributes are serialized by the DOM rather than hand-built.

### Validation of this addition

Independent Python 3.11 runs (`DB_BACKEND=sqlite PG_DSN=`), without optional local PG lab
packages installed, compared the unchanged `10cb46a` base against this addition:

- Base full suite: **1212 passed, 22 failed, 133 skipped; 560 subtests passed**.
- Changed full suite: **1214 passed, 22 failed, 133 skipped; 560 subtests passed**.
- Failure-ID sets are identical, including all 4 subfailures; full suite is not all green.
- Focused precision, supply/reset, golden, security, UI, Toshiba, hybrid, yield and OID
  selection: **127 passed, 1 skipped; 60 subtests passed**.
- `npm test`: **20 passed**, including colour gating, unrounded source preference,
  missing/zero/invalid/unsafe counts, safe malicious strings, close/cancel/backdrop,
  focus restoration, dialog reuse, no network call on opening and asset load order.
- Native dialog focus trapping/layout still require a real-browser acceptance check;
  jsdom tests emulate the native show/close API but do not prove browser layout or tab trapping.
- Test split guard: **1086 unittest-style tests assigned exactly once**; OID document and
  coverage checks and `git diff --check` passed. No change to the OID matrix was needed.

The precision regression exercises both real Toshiba collection paths and an authenticated
printer API response with non-round millionths, plus a large raw integer on the fallback
path. Existing golden output and supply-count/reset tests pass unchanged. No production
commands or deployment are executed as part of this addition.
