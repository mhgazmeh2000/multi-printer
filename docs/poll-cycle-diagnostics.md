# Poll-cycle display and split-counter evidence

## Scope and invariants

This change does not alter `POLL_INTERVAL`, SNMP retry counts/read order, page arithmetic,
colour attribution, counter acceptance/rollback, cartridge tracking, reset or yield formulas.
It adds no SNMP requests, migration, maintenance job, historical rewrite, or remote access.
Unknown colour remains unknown; a diagnostic is evidence, not an automatic correction.

## Split diagnostics

New uncertain `PRINT` events (and confirmed estimated `PRINT_GAP` events) include
`details.counter_diagnostic` in the existing log JSON. No separate per-poll event is created.
Normal reliable prints have no diagnostic. Existing events cannot acquire missing raw evidence
retroactively. Existing API permissions continue to apply.

The version-1 payload contains:

- `reason`: `split_unreliable` or `overflow_confirmed` (the latter is still estimated).
- `previous`: total/full_color/black_white **validator anchors**. Partial anchors can be older
  than the immediately preceding poll because the existing rollback guard retains them.
- `current`: the values supplied to the event validator, possibly clamped or derived by the
  existing collector. These are not mislabelled as raw SNMP responses.
- `delta`: the original total/colour/BW deltas captured **before** fallback/invariant code
  overwrites the internal split allocation. Colour/BW retain the existing calculation's zero
  for a missing pair; inspect the corresponding nulls, not zero alone, to detect missing data.
- `previous_snapshot_at`: the previous snapshot's update timestamp, **not** an asserted
  per-counter SNMP read timestamp.
- `poll_started_at`: existing poll-start UTC timestamp, if the caller provides it.
- `reads`: enhanced-path counter read windows (`started_at`, `finished_at` in UTC), source
  identifiers and integer `raw_value` (null for unavailable/non-numeric values). A raised read
  records only `read_failed: true` and re-raises the original exception. Credentials, request
  arguments and exception messages are never copied into this evidence.

The enhanced collector records its existing standard total/colour attempts and Toshiba
`print_total`, `print_fc`, `print_bw` reads (at most seven windows). These windows wrap an
existing fallback read, so may encompass that function's existing retries. They are **not**
precise printer-side update times and do not prove an atomic snapshot. On another collector
path, `reads` can be empty; no artificial times are fabricated. Diagnostics are persisted only
on the uncertain events, using the existing `logs.details` field, not in counter state.

Read through `/api/printer/<ip>/log?limit=500` or `/api/logs/all?limit=2000`, find the uncertain
print and retain the whole event including `details.counter_diagnostic`. `total` in the all-logs
response is the number of rows returned, not the total historical row count.

## Honest server-cycle indicator

`/api/status` adds `server_time`, `started_at` and `next_poll_at` to the existing count/last/
is_polling/interval fields. Stats are snapshotted under the existing data lock. `next_poll_at`
is published by the **automatic loop's actual sleep** and cleared when that sleep ends; it is
not guessed as `last + interval`. A manual cycle does not reschedule that sleep. The automatic
loop can wait behind a manual cycle's existing lock. An unset deadline is displayed as waiting.
No scheduling behaviour is changed (including the existing startup sleep).

The dashboard's former independent 40-second reload is replaced by `poll-cycle.js`:

- Countdown uses the supplied server clock/deadline, not the browser's absolute clock.
- Active collection shows an indeterminate animated bar and Persian running text, never a
  fabricated completion percentage. Reduced-motion preferences disable the animation.
- A small `/api/status` request checks active cycles about every three seconds, otherwise at
  most ten seconds apart and near the automatic deadline. It never triggers `/api/poll/now`.
- Changes to completion count/last refresh printers and logs, even if a short/manual cycle
  happened entirely between checks. A running-to-idle transition also refreshes after a failed
  cycle. Completion detection latency is usually up to three seconds while active, up to ten
  seconds for an entirely missed short cycle, plus API/render time; it is not server push.
- Compare status against the **rendered printer response's** metadata to handle requests
  straddling a completion. Skipped/failed full refreshes retry until a new snapshot is accepted.
- Repeated wakes cannot overlap watcher requests. Existing full-printer/log in-flight guards
  remain. Printer/status full loads and log loads time out at 30s, watcher requests at 15s.
- Network failures back off to 60s; returning to the tab or receiving an online event wakes the
  watcher. Authentication loss stops it and redirects to login. The manual Pull button shares
  this watcher instead of maintaining a second polling loop.
- Existing startup warmup/incremental card publication and explicit action refreshes remain.

## Deployment / acceptance

No server action is performed by this PR. Deploy only after review, normal backup and an
explicit server-update step. No migration or recalculation is required. Default `ASSET_VERSION`
is bumped; if production overrides it, update that override as part of deployment to avoid
loading a new dashboard with an old/missing helper (do not share the `.env`).

After deployment verify countdown, automatic completion, manual Pull, background-tab return
and network recovery in a real browser. Compare before/after page totals and collect a few
new uncertain events, particularly from the device with negative split deltas. Only then
consider a separately reviewed counter-read strategy; neither poll interval nor unknown
colour labels are changed by this work.

## Validation in this change

- New Python regression tests: 14 passed (raw/negative/missing splits, unchanged arithmetic
  and snapshots, real log/API round-trip, enhanced read instrumentation, confirmed gap,
  scheduler deadline/manual invariance, exception cleanup and status fields).
- JavaScript suite: 34 passed, including deterministic cycle/timer/race/recovery tests and
  jsdom execution of the production dashboard indicator renderer.
- Complete local suite: 1,228 passed, 133 skipped, 560 subtests passed, 22 failed.
  Archived base `423671e`: 1,214 passed, 133 skipped, 560 subtests passed, the **same 22**
  failure/subfailure IDs (18 FAILED + 4 SUBFAILED). Failures concern existing backup tool
  availability, timestamp/readiness expectations, old JSONB/schema expectations and
  PostgreSQL migration tests requiring unavailable local services/SQLite fixture tables.
  This is a baseline comparison, not a claim that the full suite is green.
- OID matrix regenerated for source-line shifts only; coverage 136/136, test-split guard
  1,100 unittest-style tests assigned exactly once, and `git diff --check` passed.
- Real production browser/device acceptance remains a post-deployment step.
