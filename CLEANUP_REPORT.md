# CLEANUP_REPORT — PrintGuard `refactor/comments-and-cleanup`

Branch `refactor/comments-and-cleanup`, based on `main` at `48370f4` (fix(css) topbar colours). Work done in order: **A** (translate comments and add file headers), **B** (dead code), **C** (formatting), plus docstrings and a final check. No runtime behaviour, public API, route URL, DB schema or config key was changed on purpose.

## 1. Summary

| Item | Result |
|---|---|
| Commits on top of `main` | 8 (section 2) |
| Files changed | 202, excluding this report (0 deleted, 0 added; full list in section 11) |
| Files deleted | 0 — no backup, orphan or never-imported source file was found (see section 5) |
| Pytest (`tests/`) | Before: 21 failed, 760 passed, 76 skipped. After: **same 21 failures, same IDs**, 760 passed, 76 skipped |
| Pytest `-m pytest_only` (CI) | 33 passed, 820 deselected — unchanged |
| unittest discover (CI) | Ran 624: failures=7, errors=2, skipped=75 — **identical failure list** |
| `tools/record_golden.py --only all --check` | exit 0 |
| `build_snmp_yml.py --check`, `build_oid_matrix.py --check` | both exit 0 |
| Push / PR | **Not done**: the sandbox has no GitHub credentials (`could not read Username for https://github.com`). Patch series and bundle provided instead (section 10). |

## 2. Commits

| Commit | Subject | Shortstat |
|---|---|---|
| `96e5c5d` | docs: translate Persian comments and docstrings to English | 169 files changed, 6811 insertions(+), 6811 deletions(-) |
| `5b8394c` | docs: add file headers and update file:line references | 199 files changed, 2692 insertions(+), 598 deletions(-) |
| `93aec1d` | chore: remove dead code and unused imports | 50 files changed, 160 insertions(+), 370 deletions(-) |
| `e7b683c` | style: group imports and strip trailing whitespace | 58 files changed, 370 insertions(+), 344 deletions(-) |
| `e3fc187` | docs: add docstrings to production modules, classes and functions | 79 files changed, 717 insertions(+), 149 deletions(-) |
| `df1e2cb` | docs: translate the remaining Persian comments in JS helper scripts | 2 files changed, 14 insertions(+), 14 deletions(-) |
| `da45b87` | docs: add CLEANUP_REPORT | 1 file changed, 372 insertions(+) |
| `c9b7031` | style: remove extra blank line at end of file | 3 files changed, 3 deletions(-) |

Each commit is self-contained: line-number references were remapped and the generated files (`docs/oid-matrix.md`, `monitoring/snmp.yml`) regenerated with their own tools in the same commit that moved the lines.

## 3. Task A — comments, docstrings, headers

- **Translation (commit 1).** Persian comments and docstrings in all Python files (150 files), and in JS, CSS, HTML templates, YAML, INI, requirements, `.gitignore`, batch (`REM`/`::`) and PowerShell files were translated to English. Technical terms, OIDs, file names and identifiers were kept. Translation was machine-based (Google translate endpoint), with a cache and AST/syntax checks (AST equal before and after for every Python file; YAML data identical; `node --check` for JS; `.bat` files keep CRLF; `.ps1` keeps its BOM).
- **Persian string literals were not translated** (UI text, error messages, data labels). Changing them would change behaviour, as the task required.
- **Headers (commit 2).** Every Python module has a header with `@file`, `@brief`, `@details` (responsibilities, main API, side effects), `@dependencies`, `@notes`. The headers were generated from the code (AST: public API, imports, side effects from imports and calls, environment variables read) and the one-line purpose was written per file. Migrations (`alembic/versions/*.py`) get a short origin header only. JS/CSS/HTML use `/* */` and `{# #}` blocks; YAML/INI/PowerShell use `#`; batch files use `REM`.
- **Docstrings (commit `e3fc187`).** 17 production modules, 7 classes and 283 module-level functions or public methods now have docstrings. Nested closures and test code are not covered (see section 8).
- **Test change (commit 1).** `tests/test_build_snmp_yml.py` and `tests/test_compare_snmp_sources.py` asserted Persian sentences in comments; the needle strings now use the English wording. The assertions themselves are unchanged.
- **Not done:** a systematic rewrite of "what" inline comments into "why / how to use" comments. Inline comments were translated, not rewritten. The new headers do explain purpose and usage.

## 4. Task B — dead code

**Removed (commit `93aec1d`, 29 import lines touched across the Python code)**

- Unused imports: ruff F401 reported 33 findings. Removed, except the cases listed under "kept".
- Unused local variables: ruff F841 reported 13 findings. Removed where the value was not computed by a call. `except ... as e` names that were never used were dropped.
- Unreachable code: a duplicate block after `return result` in `core/cartridge_change_engine.py::evaluate_cartridge_change` (lines 307–340). It repeated the live logic above it. Removed.
- Unused functions and methods (grep-verified, no references in code, tests, templates or config): `queue_size`, `classify_device_detailed`, `parse_snmp_response`, `_clear_cached_version`, `PrevStore.is_initialized`, `User.is_admin`, `User.is_manager`, `User.is_viewer`, `User.can_access_office`, and the test helpers `execute_steps`, `load_fixture` in `tests/golden_harness.py`.
- Unused module constants: `FROZEN_NOW` (tests/golden_harness.py), `PDU_METRICS` (tools/compare_snmp_sources.py, with its orphan comment), `ASSET_PATTERNS` (tools/find_timestamp_consumers.py), `TABLE_WEIGHTS` (tools/generate_cutover_fixture.py), `EXTRA_TABLES` (tools/test_pg_connection.py).
- Dead assignments with no side effect: three `scan = …` values in `core/collectors/canon.py`, one unused `val` and one unused `base` in tests, and an unused `exit_code` binding (the call itself is kept).
- f-strings without placeholders (10, behaviour identical).

**Restored after review**

- `config/settings` import in `create_admin.py`: ruff flagged it as unused, but the import runs the SECRET_KEY check. Kept with a `noqa` and a comment.
- `_parse_dt` in `core/yield_engine.py`: its own docstring says it is kept on purpose as a shared helper for future readers. Kept (and its `as_utc_datetime` import).

**Kept on purpose (flagged for the owner, not removed)**

- `core/collectors/base_enhanced.py` re-exports `collect_enhanced`, which `core/poller.py` imports from it. Ruff reports it as unused, but it is a deliberate bridge.
- `core/collectors/hp.py` `scan_mono`/`scan_color`: the SNMP reads are performed but never used. Removing the assignment would also remove network traffic, so it stays until the owner decides.
- `core/enhanced_collector.py` `override_color`: `prev_override.get(...)` could raise if `prev_override` is None, so removing it could change behaviour.
- Flask route handlers, hooks, error handlers and pytest/unittest hooks, even when no Python code calls them by name (the frontend calls routes by URL, e.g. `api_update_printer`, the sensor-alert endpoints).
- Flask-Login attributes (`login_view`, `login_message`), SSL attributes (`check_hostname`, `verify_mode`), openpyxl style attributes, and `load_tests` (unittest protocol).
- Function parameters that look unused (`metric_type`, `icon`, `exc_tb`, test fake signatures) — removing them would change call signatures.

**Flagged (possibly intended but not wired in) — owner decision needed**

- `core/database.py::prune_old_print_logs` and `core/security_audit.py::cleanup_old_events`: retention jobs that nothing calls. `run.py` imported `prune_old_print_logs` but never used it (import removed in commit `93aec1d`; the function is kept). `docs/` mentions both, so they look like features that were never scheduled. Wire them up or delete them.
- Unused threshold or policy constants, which may indicate missing validation: `core/collectors/base.py` `MAX_REASONABLE_DELTA`, `MIN_VALID_TOTAL_FOR_FIRST_POLL`; `core/collectors/cartridge_discovery.py` `QUEUE_RECHECK_SECONDS`; `core/cartridge_change_engine.py` `CONFIDENCE_SCORE`; `core/oid/validator.py` `VALID_NEGATIVE_VALUES`; `core/enhanced_collector.py` `DEFAULT_CARTRIDGE_YIELD`; `core/yield_engine.py` `SNAPSHOT_MIN_SECONDS`; `core/security_audit.py` `RATE_LIMIT_HIT`.
- `config/settings.py` `SNMP_PORT`: a config key that nothing reads. Config keys were not changed, so it stays.
- `core/snmp/oid_map.py` `TONER_LEVEL`: an OID constant. `tools/build_oid_matrix.py` reads OIDs from the code, so removing it would change the generated matrix. Kept.
- `tools/api_example.py`: a standalone example script with its own usage examples, not referenced anywhere. It is documentation-like, so it was not deleted.

**Tests changed (not deleted, skipped or added)**

- `tests/test_cartridge_change_timezone.py::test_both_timestamp_lines_are_locked`: it required two `utc_now_iso` call sites, one of which was the removed dead copy. The threshold is now the one live call site. The import check and the "no local now()" check are unchanged.
- `tests/test_import_db_guard.py::test_guard_is_the_first_statement_of_confirm_import`: it now skips a leading docstring before checking the first statement. The guard assertion is unchanged.

**Analyzer status**

- ruff F401/F841/F821/F541: from 46 + 10 findings to 5. The 5 left: the re-export above, two SNMP reads, `override_color`, and one bug (below).
- vulture (≥60%): 189 candidates before, 173 after. The remainder is the framework-registered code listed above.
- pyflakes: one additional bug line (below).

## 5. Files deleted / flagged

- **Deleted: none.** No `*.bak`, `*.orig`, `*_old.*`, `*~` or other backup file is tracked. Every Python module is imported somewhere or referenced by name (CI, bat files, docs).
- **Flagged:** `tools/api_example.py` (see section 4).
- **Not deleted, but not orphans:** `tests/fixtures/*` are loaded by path at runtime; `grafana/provisioning/*.yml` is read by Grafana.

## 6. Task C — formatting

- **Line endings:** the repository already stores text as LF (`.gitattributes`: `* text=auto eol=lf`). `.bat` files stay CRLF in the working tree (`eol=crlf`). Checked: no bare LF in `.bat` files.
- **Trailing whitespace:** stripped in `.py`, `.js`, `.css`, `.html` (201 lines). Lines that end inside a multi-line string or template literal were left alone, because their characters are runtime data (20 Python lines, 2 JS lines). Markdown was not touched, because trailing double spaces are line breaks there.
- **Imports (isort style):** the top-level import block of each Python file was grouped into `__future__`, stdlib, third-party, local. Stdlib and third-party are sorted. Local imports keep their order, because of import cycles. Statements are moved verbatim (no re-wrapping, no splitting). 46 files changed, about 130 lines. Function-level imports were not touched. With the default isort settings, 86 files would change (about 800 diff lines), so the narrower grouping was used instead.
- **Indentation:** existing indentation was kept in every file. Python is 4-space everywhere. JS, CSS and HTML mix 2- and 4-space (for example `dashboard.js`, `security.html`, `users.html`), so re-indenting them would have produced massive diffs. The rule "unless the file differs" was applied.
- **Commented-out code:** none found. Python comments that parse as code were checked one by one: they are annotations (for example `# Printer(large)`, `# hits=1`), not dead code. JS, CSS and HTML contain only prose comments.

## 7. Test results — before and after

| Run | Before (`main` 48370f4) | After (final commit) |
|---|---|---|
| `pytest tests/` | 21 failed, 760 passed, 76 skipped, 84 subtests passed | 21 failed, 760 passed, 76 skipped, 84 subtests passed — **same failing IDs** |
| `pytest tests/ -m pytest_only` | 33 passed, 820 deselected | 33 passed, 820 deselected |
| `unittest discover -s tests` | Ran 624; FAILED (failures=7, errors=2, skipped=75) | Ran 624; FAILED (failures=7, errors=2, skipped=75) — same list |
| `tools/record_golden.py --only all --check` | not run before | exit 0 |
| `tools/build_snmp_yml.py --check`, `tools/build_oid_matrix.py --check` | — | exit 0 |
| Import of all 91 application modules | — | all import (run.py and create_admin.py skipped: they start a server or prompt) |

The 21 pre-existing failures are environmental, not caused by this work. The sandbox has no PostgreSQL on 127.0.0.1:5432 and no `pg_dump`/`psql`, and the copied `logs.db` lacks the expected tables. They are:

- `tests/test_backup_pg.py::VerifyDumpTests::test_empty_toc_listing_is_rejected`
- `tests/test_backup_pg.py::VerifyDumpTests::test_pg_dump_is_called_without_password_on_the_command_line`
- `tests/test_backup_pg.py::VerifyDumpTests::test_table_data_lines_alone_do_not_count_as_tables`
- `tests/test_cutover_readiness.py::TimestampsTests::test_after_cutover_z_is_green`
- `tests/test_jsonb_columns.py::SqliteSchemaUnchangedTests::test_logs_details_is_still_text_on_sqlite`
- `tests/test_migrate_sqlite_to_pg.py::TestEndToEnd::test_dry_run_counts_all_tables`
- `tests/test_migrate_sqlite_to_pg.py::TestEndToEnd::test_end_to_end_success`
- `tests/test_migrate_sqlite_to_pg.py::TestEndToEnd::test_end_to_end_with_skips`
- `tests/test_migrate_sqlite_to_pg.py::TestEndToEnd::test_insert_after_migration_succeeds_without_explicit_id`
- `tests/test_migrate_sqlite_to_pg.py::TestEndToEnd::test_verify_after_migration`
- `tests/test_migrate_sqlite_to_pg.py::TestNaiveTimestampHandling::test_invalid_timestamp_skipped_always`
- `tests/test_migrate_sqlite_to_pg.py::TestNaiveTimestampHandling::test_mixed_z_and_naive`
- `tests/test_migrate_sqlite_to_pg.py::TestNaiveTimestampHandling::test_naive_counter_resets_between_runs`
- `tests/test_migrate_sqlite_to_pg.py::TestNaiveTimestampHandling::test_naive_timestamp_converted_with_flag`
- `tests/test_migrate_sqlite_to_pg.py::TestNaiveTimestampHandling::test_naive_timestamp_skipped_by_default`
- `tests/test_migrate_sqlite_to_pg.py::TestNaiveTimestampHandling::test_threshold_100_naive_triggers_fatal`
- `tests/test_migrate_sqlite_to_pg.py::TestProductionLikeE2E::test_end_to_end_with_real_production_format`
- `SUBFAILED(column='printer_counters.cartridge_id_state') tests/test_jsonb_columns.py::SqliteSchemaUnchangedTests::test_the_python_side_columns_are_text_on_both_backends`
- `SUBFAILED(column='printer_counters.toner_overrides') tests/test_jsonb_columns.py::SqliteSchemaUnchangedTests::test_the_python_side_columns_are_text_on_both_backends`
- `SUBFAILED(column='users.allowed_modules') tests/test_jsonb_columns.py::SqliteSchemaUnchangedTests::test_the_python_side_columns_are_text_on_both_backends`
- `SUBFAILED(column='users.allowed_offices') tests/test_jsonb_columns.py::SqliteSchemaUnchangedTests::test_the_python_side_columns_are_text_on_both_backends`

## 8. Scope decisions and known limits

- **Persian documentation (decision needed).** `docs/*.md`, `README.md`, `README.en.md` and some JSON files still contain Persian prose (about 45 files). Translating them is a separate, large effort (100–500 lines per document) and was out of scope for this branch. Recommended: a separate docs-translation branch.
- **Persian string literals in Python and templates are intentionally unchanged** (UI text, messages). About 1,700 string tokens. Changing them would change behaviour.
- **Migrations** (`alembic/versions/*.py`) keep their Persian comments and docstrings (26 comment lines, 5 docstrings). They are history and only got the short origin header.
- **`monitoring/snmp.yml`** has no header (it is generated; `--check` compares it byte for byte). Its Persian header comments come from string literals in `tools/build_snmp_yml.py`. Translating those literals and regenerating is a one-line change per comment in the tool, not a hand edit of the YAML.
- **`dashboard.js` lines 1588–1626** contain HTML comments inside JS string literals that are rendered into the page. They are runtime data, so they stay.
- **Headers, not rewritten:** `alembic/script.py.mako` (a template; a header would be copied into every new migration), `monitoring/snmp.yml` (see above), `requirements*.txt`, `.gitignore`, JSON data files, `tests/fixtures/*`, and `web/static/js/Sortable.min.js` (vendored; it got a short header after its licence banner).
- **Machine translation quality.** Comments were machine-translated. Some are literal and read awkwardly, especially multi-line prose split across lines (for example in `monitoring/generator.yml`). Some specific terms were also translated loosely (for example one "Arafaq" term and one "passwords" term). A human proofreading pass of comments is recommended.
- **Line references in prose documentation** (`docs/*.md`, `README`) that cite `file:line` were not updated. They are historical notes and may be a few lines off. The generated matrix and the generator spec were updated.
- **Test code docstrings** are not complete: 84 test classes and about 260 test helper functions have none.

## 9. Bugs found (not fixed — the task forbids runtime behaviour changes)

1. **`core/sensor_alert.py` — sensor alert e-mail body always fails.** The HTML string contains `width:100%;` and is formatted with `%`, so `% alert_rows` raises `ValueError: unsupported format character ';'`. Fix: write `100%%`. **Priority: high** — the alert e-mail cannot be built.
2. **`core/collectors/toshiba.py` ~line 258 — undefined name `snmp_version`.** It raises `NameError` when that branch runs. Found by ruff F821 and pyflakes.
3. **`core/collectors/hp.py`** reads two SNMP values that are never used (see section 4). Extra network traffic on every poll.

## 10. Delivery

- **Push and PR:** not possible from this sandbox (no GitHub credentials). To publish from a machine with access:

  ```
  git fetch origin && git checkout refactor/comments-and-cleanup
  git push -u origin refactor/comments-and-cleanup
  # then open a PR: base main <- refactor/comments-and-cleanup
  ```

- **Patch series and bundle** are in `/home/user/deliverables/`: one `.patch` per commit (`git am` onto `main` at `48370f4`) and `printguard-refactor.bundle` (`git fetch /path/printguard-refactor.bundle refactor/comments-and-cleanup`).
- **Git identity:** the repository had no `user.name`/`user.email`. Commits use the placeholder `Arena Agent <agent@arena.ai>`. To change it before publishing: `git rebase -r main --exec 'git commit --amend --no-edit --reset-author'`.

## 11. Files changed on the branch (202, before this report)

Relative to `main` (`48370f4`), excluding `CLEANUP_REPORT.md` itself.

- `.github/workflows/tests.yml`
- `.gitignore`
- `alembic.ini`
- `alembic/env.py`
- `alembic/versions/0001_initial_schema.py`
- `alembic/versions/0002_boolean_columns.py`
- `alembic/versions/0003_identity_columns.py`
- `alembic/versions/0004_jsonb_columns.py`
- `alembic/versions/0005_add_legacy_log_columns.py`
- `cartridge_test.bat`
- `config/__init__.py`
- `config/settings.py`
- `core/__init__.py`
- `core/cartridge_change_engine.py`
- `core/collectors/__init__.py`
- `core/collectors/base.py`
- `core/collectors/base_enhanced.py`
- `core/collectors/brother.py`
- `core/collectors/canon.py`
- `core/collectors/cartridge_discovery.py`
- `core/collectors/cartridge_id.py`
- `core/collectors/exporter_adapter.py`
- `core/collectors/hp.py`
- `core/collectors/sensor.py`
- `core/collectors/toshiba.py`
- `core/database.py`
- `core/db/__init__.py`
- `core/db/connection.py`
- `core/db/exceptions.py`
- `core/db/placeholders.py`
- `core/db/schema.py`
- `core/db/scripts.py`
- `core/db/upsert.py`
- `core/device_classifier.py`
- `core/enhanced_collector.py`
- `core/exporter_client.py`
- `core/groups.py`
- `core/oid/__init__.py`
- `core/oid/catalog.py`
- `core/oid/scanner.py`
- `core/oid/validator.py`
- `core/poller.py`
- `core/security_audit.py`
- `core/sensor_alert.py`
- `core/snmp/__init__.py`
- `core/snmp/oid_map.py`
- `core/snmp/protocol.py`
- `core/store.py`
- `core/time_utils.py`
- `core/yield_engine.py`
- `create_admin.py`
- `docs/oid-matrix.md`
- `grafana/provisioning/dashboards/dashboards.yml`
- `grafana/provisioning/datasources/prometheus.yml`
- `migrate.bat`
- `models.py`
- `monitoring/generator.yml`
- `monitoring/snmp.yml`
- `pytest.ini`
- `requirements-dev.txt`
- `requirements.txt`
- `run-watchdog.bat`
- `run.py`
- `setup.bat`
- `start.bat`
- `stop-project.bat`
- `tests/conftest.py`
- `tests/fake_snmp.py`
- `tests/golden_harness.py`
- `tests/golden_profiles.py`
- `tests/golden_scenarios.py`
- `tests/migration_graph.py`
- `tests/test_alembic_ini_encoding.py`
- `tests/test_auth_next_redirect.py`
- `tests/test_auto_migrate.py`
- `tests/test_backup_pg.py`
- `tests/test_brand_supplies.py`
- `tests/test_build_snmp_yml.py`
- `tests/test_cartridge_alias.py`
- `tests/test_cartridge_change_event.py`
- `tests/test_cartridge_change_timezone.py`
- `tests/test_cartridge_discovery.py`
- `tests/test_cartridge_identity.py`
- `tests/test_collation_ordering.py`
- `tests/test_compare_snmp_sources.py`
- `tests/test_counter_restore.py`
- `tests/test_counters_event.py`
- `tests/test_create_admin.py`
- `tests/test_cutover_readiness.py`
- `tests/test_cutover_rehearsal.py`
- `tests/test_db_adapter.py`
- `tests/test_db_migration_columns.py`
- `tests/test_ddl_bootstrap.py`
- `tests/test_diagnose_data_quality.py`
- `tests/test_exceptions_pg.py`
- `tests/test_exporter_adapter.py`
- `tests/test_exporter_backoff.py`
- `tests/test_exporter_client.py`
- `tests/test_generated_columns_migration.py`
- `tests/test_generator_yml.py`
- `tests/test_generic_cartridge_change_engine.py`
- `tests/test_golden.py`
- `tests/test_groups.py`
- `tests/test_hp_toner_key.py`
- `tests/test_hybrid_full_path.py`
- `tests/test_import_db_guard.py`
- `tests/test_install_ps1.py`
- `tests/test_integration_cartridge_pipeline.py`
- `tests/test_json_extract.py`
- `tests/test_jsonb_columns.py`
- `tests/test_logging_simulation.py`
- `tests/test_metrics.py`
- `tests/test_metrics_auth.py`
- `tests/test_metrics_pg.py`
- `tests/test_migrate_sqlite_to_pg.py`
- `tests/test_migrate_timestamps.py`
- `tests/test_offline_resilience.py`
- `tests/test_oid_matrix.py`
- `tests/test_paper_size.py`
- `tests/test_poll_event_invariant.py`
- `tests/test_poller_exporter_flag.py`
- `tests/test_preflight_data_quality.py`
- `tests/test_proxy_fix.py`
- `tests/test_rbac_admin_guard.py`
- `tests/test_route_logging.py`
- `tests/test_smoke_routes.py`
- `tests/test_snmp_yml.py`
- `tests/test_strictness_pg.py`
- `tests/test_time_utils.py`
- `tests/test_timestamp_migration.py`
- `tests/test_toner_override_per_color.py`
- `tests/test_toshiba_fallback_sync.py`
- `tests/test_ui_assets.py`
- `tests/test_upsert_cross_backend.py`
- `tests/test_yield_engine.py`
- `tools/analyze_old_events.py`
- `tools/api_example.py`
- `tools/audit_pages.py`
- `tools/auto_migrate.py`
- `tools/build_oid_matrix.py`
- `tools/build_snmp_yml.py`
- `tools/cartridge_test_step2.py`
- `tools/check_cutover_readiness.py`
- `tools/check_oid_coverage.py`
- `tools/cleanup_fake_gaps.py`
- `tools/compare_snmp_sources.py`
- `tools/diagnose_data_quality.py`
- `tools/diagnose_printer.py`
- `tools/export_from_pg.py`
- `tools/find_insert_or_replace.py`
- `tools/find_sqlite_excepts.py`
- `tools/find_timestamp_consumers.py`
- `tools/generate_cutover_fixture.py`
- `tools/get_oauth_token.py`
- `tools/init_pg_database.py`
- `tools/install-startup-task.bat`
- `tools/install-weekly-audit.bat`
- `tools/migrate_sqlite_to_pg.py`
- `tools/migrate_timestamps_to_utc.py`
- `tools/probe_cartridge_id.py`
- `tools/record_golden.py`
- `tools/scan_golden_for_pii.py`
- `tools/set_cutover_marker.py`
- `tools/test_ddl_bootstrap.py`
- `tools/test_dual_backend.py`
- `tools/test_pg_connection.py`
- `tools/verify_server_timezone.py`
- `utils.py`
- `web/__init__.py`
- `web/auth.py`
- `web/routes/__init__.py`
- `web/routes/dashboard.py`
- `web/routes/discover.py`
- `web/routes/export_bp.py`
- `web/routes/groups.py`
- `web/routes/import_db.py`
- `web/routes/logs.py`
- `web/routes/metrics.py`
- `web/routes/printers.py`
- `web/routes/scan.py`
- `web/routes/security.py`
- `web/routes/stats.py`
- `web/routes/system.py`
- `web/routes/users.py`
- `web/routes/validation.py`
- `web/routes/yield_status.py`
- `web/security.py`
- `web/static/css/style.css`
- `web/static/js/Sortable.min.js`
- `web/static/js/chart.umd.min.js`
- `web/static/js/dashboard.js`
- `web/static/js/legacy-mode.js`
- `web/templates/base.html`
- `web/templates/dashboard.html`
- `web/templates/forgot_password.html`
- `web/templates/login.html`
- `web/templates/pending_verification.html`
- `web/templates/register.html`
- `web/templates/reset_password.html`
- `web/templates/security.html`
- `web/templates/users.html`
- `windows/install-snmp-exporter.ps1`
