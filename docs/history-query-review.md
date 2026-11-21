# History-query and security follow-up

## Scope and deployment gate

This working-tree follow-up is based on `da48f0e072d6b93a74995a062301ac2912a81ff0`.
It does **not** deploy anything, activate derived history, install source triggers/indexes,
change the page-consumption formula, round API values, change polling cadence, increase
timeouts, or truncate history. The Windows isolated-copy lock-observer result is still
pending. Production remains on the previously reported `9f9d933` with the derived reader off.

## Implemented changes

* Sensor alert delivery: escape the literal percent in the formatted CSS table and
  HTML-escape dynamic values. Tests reach the actual composition/send path with SMTP
  mocked, including cooldown, disabled/normal readings, humidity, injection payloads,
  and transport failure.
* Printer APIs: remove community credential keys recursively from non-admin list/detail
  telemetry, including the pending-first-poll configuration fallback. Admin behavior,
  authentication, office restrictions, and collector state are preserved.
* History query: replace the endpoint OR-join with a `picked` CTE and ID membership over
  the union of first/last endpoints. Ordering, projection, window/change detection,
  filter bindings and the single limit parameter remain unchanged. SQLite and owned
  PostgreSQL tests compare ordered rows with both the old query and an independent
  Python oracle. PostgreSQL tests also exercise empty/unready, ready, and disabled
  derived-reader branches. Maintenance writes occur **only in disposable test DBs**.
* Add `tools/diagnose_yield_history.py` as a replacement for the unsafe attached
  diagnostic. It imports neither the application nor settings; it does not infer a DSN.

## Read-only diagnostic boundaries

The diagnostic accepts either an explicit SQLite file or the previously approved
PostgreSQL copy's existing local authentication file. It never creates that auth file.
PostgreSQL credentials must contain exactly host/port/user/dbname/password, with target
pinned to `127.0.0.1:55439`, `yield_activation_copy`, `yield_activation_lab`. It scrubs
inherited libpq variables during connection and verifies the live system identifier,
data directory, server address/port, database/user, and transaction read-only property
before reading telemetry. The caller must supply the expected copy identity/path.

SQLite uses URI `mode=ro`, `query_only`, and a read transaction. PostgreSQL uses startup
default read-only and an explicit repeatable-read/read-only transaction. Every query
has a deadline (default/max 120 seconds), lock waits are bounded (1 second), connection
setup is bounded (5 seconds), and the comparison has a ten-minute server-work budget.
There are at most three alternating pairs and at most 256 slots. Deadlines are not
hard process-kill guarantees for OS/network stalls or Python memory/serialization work.

Reports contain counts, timings, hashes, equality, and optional aggregate PostgreSQL
EXPLAIN node counts/costs. They do not contain telemetry rows, raw plans, credentials,
exception text, or connection strings. EXPLAIN is **not ANALYZE**. Failures stop the
comparison; rollback and close are attempted independently. No migrations, ANALYZE,
service start/stop, activation, or maintenance writes are performed. Read-only queries
still consume resources and acquire ordinary read locks; use the approved stopped-copy
workflow, not production. This tool is not a full API or concurrent-write benchmark.

No new Windows command is prescribed here. Fetching the exact reviewed source, verifying
its hash, starting only the approved copy, and executing the comparison remain a separate
collaborative step with Persian explanations before each command block. The old attached
diagnostic must not be substituted.

## Controlled PostgreSQL baseline investigation

The same four review test files were executed against exact source snapshots
`882af2a726de91dca0653e3d0dd5ea3a4b321034`, base `da48f0e`, and the initial working tree,
using an owned disposable PostgreSQL 16.2 cluster with explicit `C` collation. All three
reproduced the same failures: stale migration-head assertion, SQLite schema tests reading
the checkout's runtime `logs.db`, cutover test relying on Git discovery in source archives,
and an incomplete static text-sort allowlist. No new failure appeared in this comparison.
The initial working result was 9 failed / 85 passed / 1 skipped (subtest accounting included).

Narrow test-only repairs now use an owned SQLite schema fixture, expect head `0009`,
provide an explicit cutover boundary, and document the three existing metadata/identifier
sort sites. The BOOLEAN assertion accounts explicitly for later optional dashboard and
derived metadata columns when those tables exist; the production SQLite-import conversion
contract is unchanged. The corrected four-file PostgreSQL run passes:
**89 passed, 1 skipped, 21 subtests passed**. The skip is optional ICU availability.
The external non-C collation failure was not reproduced; no collation check was weakened.
This is not evidence that PostgreSQL 17 production, every external schema, or every test
in the repository is green.

## Synthetic PostgreSQL timing — not a production speed claim

Owned PostgreSQL 16.2 synthetic data, 51 slots, work_mem 4 MB, two alternating pairs:

| Input rows | Legacy seconds (runs 1/2) | Candidate seconds (runs 1/2) | Returned rows |
| --- | --- | --- | --- |
| 200,000 | 0.6376 / 0.7047 | 0.6725 / 0.6566 | 4,080 |
| 2,200,000 | 5.9550 / 5.4576 | 5.7250 / 5.5468 | 40,800 |

Ordered outputs were exactly equal in each comparison. The 2.2M-row output hash was
`fd9f051b88c21abed8ed3abb7ce09059236d679fc6f9a10ad819b0ff678d1589`.
These timings are close and order/cache sensitive: **no meaningful PostgreSQL speedup
has been established**. SQLite timings in the attached review cannot establish PostgreSQL
performance. The rewrite still scans history/window partitions; it is not an incremental
reader. Windows copy plan/time comparison remains outstanding.

## Security triage not silently treated as fixed

| Finding | Disposition |
| --- | --- |
| Sensor formatting exception / HTML interpolation | Fixed and regression tested here. |
| Non-admin printer community disclosure | Fixed at both telemetry response boundaries; role/office HTTP tests added. |
| Datasource credential storage | Masking is not encryption at rest. Unchanged; requires key lifecycle, migration, backup/recovery and deployment decisions. |
| External PostgreSQL datasource SQL | Admin-only, SELECT sanitizer, read-only transaction and query/lock/row limits already exist. Unlike internal reports, external sources have no internal-table allowlist. Do not impose app table names on arbitrary external databases blindly; use a least-privilege external DB account and review a per-source policy separately. |
| CSP `unsafe-inline` | Confirmed current policy; unchanged. Nonces/hashes and inline-template/script migration require a separate compatibility rollout. |
| Datasource error text / query builder HTML | Source review finds raw exception interpolation in REST/schema paths and unescaped query-builder cells/metadata/errors. Treat as open exposure/XSS risks; read-only SQL does not make returned HTML safe. No claim of closure in this limited patch. |
| Copy auth file cleartext warning | Still open. Local credential handling is not made encrypted by pinning the diagnostic target. Do not upload the file or credentials. |

## Final validation and remaining baseline failures

* Targeted acceptance: **77 passed, 1 skipped, 342 subtests passed**. This includes real
  disposable PostgreSQL comparisons/derived fallback plus actual read-only write refusal;
  diagnostic failure cleanup/redaction and SQLite deadline abort are also tested.
* Corrected four-file review run on owned PostgreSQL 16.2/C:
  **89 passed, 1 skipped, 21 subtests passed**.
* JavaScript suite after installing the locked dev dependencies: **40 passed**.
* Full `tests` comparison, SQLite-default environment with the same optional local PG
  dependencies and no operational DSN:
  * Base `da48f0e`: **23 failed, 1309 passed, 83 skipped, 611 subtests passed**.
  * Working tree: **16 failed, 1334 passed, 83 skipped, 656 subtests passed**.
  * Every failing working-tree testcase also failed on the base; no new failing testcase
    appeared in this comparison. This does **not** mean the full suite is green.
* Of the remaining failures, three backup tests require `pg_dump`/`pg_restore` on PATH
  even though subprocess execution is mocked. Twelve SQLite-to-PG migration tests depend
  on the checkout's mutable runtime `logs.db` fixture (missing source tables/schema).
  These environment/fixture failures are outside this limited production-code patch.
* The last failure was the generated OID document's sync gate. Its subsequent deterministic
  generation updates only the example locations of the loopback address and the document
  digest, not any real OID. With that generated document retained, the complete OID test
  file was rerun successfully: **5 passed**. The full suite has not been rerun after this
  documentation refresh; do not relabel the full-run result as 15 failures.
* `git diff --check` and compilation of the changed application/diagnostic modules pass.

The broad suite collected tests before the final PostgreSQL write-refusal assertion was added;
that assertion was covered by the final targeted run. The final targeted results supersede
the earlier focused 75-test result. No PR, deployment, production maintenance, or Windows
copy comparison is implied by these local test results.
