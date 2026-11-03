# QA Review C — Test-Coverage Gap Analysis: cPanel Ingest Pipeline (Plans 01–06)

Date: 2026-11-03
Reviewer: QA reviewer C
Scope: `backupme/core/cpanel_ingest.py`, `backupme/core/db_lifecycle.py`, `backupme/core/rebalance.py`, `backupme/scheduler.py`, `backupme/web/routes.py` (ingest/backends sections), `backupme/cli/ingest_cmd.py`, `backupme/cli/backend_cmd.py` against `tests/test_cpanel_ingest.py`, `tests/test_ops_hardening.py`, `tests/test_db_lifecycle.py`, `tests/test_rebalance.py`, `tests/test_scheduler_automation.py`, `tests/test_web_ingest.py`, `tests/test_cli_output.py`, plus `scripts/smoke_e2e.py`.

Method: full read of each product module and its test file, cross-reference of every engine branch / route / CLI flag against existing assertions, plus a keyword sweep across all 31 test files (e.g. only `tests/test_versioning.py` touches `account_pattern` / `exclude_members` / `max_member_size_gb` / `raw_backend` — and only at YAML-parsing level, never at engine level).

---

## 0. Suite status

`/c/Python313/python.exe -m pytest tests/ -q` → **exit 0, GREEN**.
**198 collected, 197 passed, 1 skipped.** The single skip is `tests/test_cli_helpers.py::test_progress_fallback_by_natural_detection` (environment-dependent progress-module detection; 's' at position 40 of the first progress line = last test of that file in collection order).

No product files were modified; no test files were created.

---

## 1. Coverage gaps

Severity legend: **High** = money / data-loss / quiet-corruption risk; **Medium** = regression risk; **Low** = polish.

### HIGH

#### H1. Encrypted ingest end-to-end — no test ingests under `enc_env`
The `enc_env` fixture exists in `tests/conftest.py` and is used only by `test_encrypted_dr.py` / `test_downloader.py`. No ingest test ever runs with `encryption.enabled: true`, so the full path staging → `upload_bundle` → AES-GCM blob → restore is unpinned for cPanel ingest. A mis-wire (e.g. dict vs dataclass config reaching `_encrypt_data`) would either crash ingest or, worse, store plaintext while the operator believes it is encrypted — quiet security failure.
**Test:** `test_ingest_tree_end_to_end_encrypted` — file: `tests/test_cpanel_ingest.py`
```python
def test_ingest_tree_end_to_end_encrypted(enc_env):
    make_cpmove(enc_env)                      # drop archive, cfg = load_config()
    out = cpanel_ingest.ingest_tree(str(enc_env / "drop"), backends_from_enc_cfg(), cfg)
    assert out["status"] == "ok" and all(r["status"] == "ok" for r in out["results"])
    assert execute("SELECT COUNT(*) FROM blobs WHERE encrypted = 1").fetchone()[0] >= 1
    assert b"<?php echo 1;" not in first_stored_blob_bytes()   # ciphertext on disk
    r = cpanel_ingest.restore_cpanel("acct", 1, out=..., backends=..., config=cfg)
    assert (Path(r["out"]) / "homedir" / "index.php").read_bytes() == b"<?php echo 1;"
```

#### H2. `max_member_size_gb` oversize skip — members silently missing from the vault
Engine branch `cpanel_ingest.py:360-364` (skip + log.warning only) has no test. Consequence of a regression or of the default 5 GB limit in production: a large mailbox/home-dir member is silently dropped, `ingest_archive`'s return payload carries no skip counter (only a log line plus the `indexed != estimated` warning), and a later restore of that generation is incomplete. Restore-gap = perceived data loss.
**Test:** `test_max_member_size_skips_large_member_silently` — file: `tests/test_cpanel_ingest.py`
```python
def test_max_member_size_skips_large_member_silently(drop_root, env, backends, ingest_cfg):
    ingest_cfg.cpanel_ingest.max_member_size_gb = 0.0000005   # ~0.5 KB
    make_cpmove(env, "acct", override_content={"homedir/config.php": b"x" * 4096})
    out = cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    assert out["results"][0]["members"] == 2                   # big member skipped
    assert not execute("SELECT 1 FROM files WHERE relative_path='homedir/config.php'").fetchone()
    # pins today's behavior: no skip field in the result — flag as product follow-up
```

#### H3. Pause never proven to defer anything (web worker); scan silently clears pause
`test_pause_resume` (`tests/test_web_ingest.py:156-162`) only flips the `_ingest_paused` Event and reads the flag back. Nothing proves `routes.py:_ingest_worker` (line 2346) actually defers archives while paused, and nothing pins that `POST /api/ingest/scan` **clears the pause** as a side effect (routes.py:2446). A regression making pause a no-op keeps ingesting (bandwidth/egress spend, staging churn) while the UI reports "paused" — money risk for an operational control.
**Test:** `test_pause_defers_archives_and_scan_clears_pause` — file: `tests/test_web_ingest.py`
```python
def test_pause_defers_archives_and_scan_clears_pause(client, env):
    _make_cpmove(env); _make_cpmove(env, name="cpmove-two.tar.gz")   # 2 archives
    client.post("/api/ingest/pause"); client.post("/api/ingest/scan", json={})
    wait_done(client)   # poll /api/ingest/status until not running
    s = client.get("/api/ingest/status").json()
    assert s["paused"] is True and execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert all(r["status"] == "deferred" and r["reason"] == "paused" for r in s["results"])
    client.post("/api/ingest/resume"); client.post("/api/ingest/scan", json={})
    wait_done(client); assert execute("SELECT COUNT(*) FROM files WHERE source_root LIKE 'cpanel/%'").fetchone()[0] == 3
```

#### H4. `scheduler.apply_retention` — untested deletion logic (scheduler.py:88-138)
`apply_retention` soft-deletes versions and unrefs blobs, with subtle date math (`days <= daily`, Monday-window weekly, `created.day <= 7` monthly). No test anywhere exercises it (keyword sweep: zero hits). An off-by-one or inverted-window regression silently deletes versions beyond policy — with `hard_purge_versions` downstream, that becomes permanent loss. (The tick wrappers `_run_retention` / `_run_purge` are tested in `tests/test_scheduler_automation.py`; the actual pruning function is not.)
**Test:** `test_apply_retention_keeps_policy_versions_soft_deletes_rest` — file: `tests/test_scheduler_automation.py`
```python
def test_apply_retention_keeps_policy_versions_soft_deletes_rest(env):
    _seed_versions("vg1", n=8, created_ats=[now, now-1d, now-3d, last_monday, now-10d, now-20d, now-40d, now-50d])
    apply_retention({"retention": {"enabled": True, "versions": 2, "daily": 7, "weekly": 4, "monthly": 12}})
    alive = {r["version_seq"] for r in execute("SELECT version_seq FROM files WHERE version_group='vg1' AND deleted_at IS NULL")}
    assert alive == {1, 2, 4}          # newest 2 + protected Monday snapshot
    assert blob_refcounts_dropped_for_deleted()   # unref_blob ran for purged chunks
```

### MEDIUM

#### M1. `account_pattern` regex — engine behavior untested (YAML parsing only)
`test_versioning.py:83-98` only asserts the field parses. `_account_from_relpath` branches (first group vs whole match, invalid regex → warning + fallback, non-match → folder fallback) are unpinned. A regression mis-attributes archives to the wrong account: files land under the wrong `cpanel/<account>` source root, generations/restore group incorrectly — quiet mis-organization that is expensive to unwind.
**Test:** `test_account_pattern_regex_extracts_and_falls_back` — file: `tests/test_cpanel_ingest.py`
```python
def test_account_pattern_regex_extracts_and_falls_back(env, monkeypatch):
    _cp_archive(env, "drop/cpmove-zed.tar.gz", _build_tar_bytes(with_symlink=False, with_traversal=False))
    monkeypatch.setenv("BACKUPME_CONFIG", _cfg_with(account_pattern=r"^cpmove-([a-z0-9]+)\."))
    assert [i["account"] for i in cpanel_ingest.discover(...)] == ["zed"]
    _cfg_with(account_pattern=r"([unclosed")           # invalid regex ignored
    assert cpanel_ingest.discover(...)[0]["account"] != "([unclosed"   # fallback path taken
```

#### M2. `exclude_members` globs — engine behavior untested
`_member_allowed` (`cpanel_ingest.py:277-283`) matches full path or basename; the ingest loop counts `excluded` but never reports it in the result payload. Only YAML parsing is tested. Regression → mail-dir/trash globs silently stop being honored (privacy + storage bloat).
**Test:** `test_exclude_members_globs_skip_matching_members` — file: `tests/test_cpanel_ingest.py`
```python
def test_exclude_members_globs_skip_matching_members(drop_root, env, backends, ingest_cfg):
    ingest_cfg.cpanel_ingest.exclude_members = ["mysql/*", "*/config.php"]
    out = cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    assert out["results"][0]["members"] == 1
    assert execute("SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'").fetchone()[0] == 1
    assert not (env / "drop" / ".done" / "acct").exists() or out["results"][0]["status"] == "ok"
```

#### M3. `keep_raw` + `raw_backend` routing — fallback-to-all-backends unpinned
`test_keep_raw_uploads_raw_archive` covers `keep_raw: 1` with no `raw_backend`. The filter branch (`cpanel_ingest.py:457-464`) — raw lands only on the named backend, and an **unknown** name falls back to uploading the multi-GB archive to **all** backends with only a warning — is untested. Misconfiguration → large raw archives replicated everywhere = real storage cost.
**Test:** `test_keep_raw_raw_backend_routes_and_falls_back` — file: `tests/test_cpanel_ingest.py`
```python
def test_keep_raw_raw_backend_routes_and_falls_back(two_backend_env, backends):
    cfg = _cfg_with(keep_raw=1, raw_backend="second")
    cpanel_ingest.ingest_tree(...); assert raw_rows_only_on("second")
    cfg2 = _cfg_with(keep_raw=1, raw_backend="no_such_backend")
    cpanel_ingest.ingest_tree(...)   # re-drop archive
    assert raw_rows_on_all_backends()   # pins documented fallback
```

#### M4. CLI `--watch` mode — zero tests
`ingest_cmd.py:134-151`: the re-scan loop, the interval sleep, KeyboardInterrupt exits ("stopped", exit 0) and the post-loop `ingest_tree` (dead code when `--watch` is given) are all untested. A regression that breaks the loop or makes it exit after one pass silently stops continuous ingest — archives pile up unprocessed.
**Test:** `test_ingest_cpanel_watch_loops_until_interrupt` — file: `tests/test_cli_output.py`
```python
def test_ingest_cpanel_watch_loops_until_interrupt(env, backends, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(ci, "ingest_tree", fake_that_counts_and_interrupts_on_2nd(calls))
    monkeypatch.setattr("backupme.cli.ingest_cmd.time.sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
    r = CliRunner().invoke(cli, ["--db", str(env / "test.db"), "ingest-cpanel", str(env / "drop"), "--watch", "--interval", "1"])
    assert r.exit_code == 0 and "stopped" in r.output and calls["n"] == 1
```

#### M5. `restore_cpanel` member_filter edge cases + always-"ok" status
Only the happy-path filter `homedir/*` is tested. Untested: filter matching nothing, unknown account/generation, empty retar. Notably `restore_cpanel` returns `status: "ok"` with `members: 0` for a nonexistent account/generation, so `restore-cpanel ghost 99` exits **0** — scripts can't detect a failed restore.
**Test:** `test_restore_no_match_and_unknown_account_edge_cases` — file: `tests/test_cpanel_ingest.py`
```python
def test_restore_no_match_and_unknown_account_edge_cases(drop_root, env, backends, ingest_cfg):
    cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    r = cpanel_ingest.restore_cpanel("acct", 1, member_filter="nomatch/*", out=..., backends=..., config=...)
    assert r["status"] == "ok" and r["members"] == 0
    r2 = cpanel_ingest.restore_cpanel("ghost", 99, out=..., backends=..., config=...)
    assert r2["status"] == "ok" and r2["members"] == 0     # pins the weak signal
    assert CliRunner().invoke(cli, ["restore-cpanel", "ghost", "99"]).exit_code == 0  # follow-up: should be 1
```

#### M6. SSE streams — `/api/ingest/stream` and `/api/backends/rebalance/stream` untested
Both `event_generator`s (routes.py:2461-2482, 2672-2692): `data: <json>\n\n` framing, change-dedup (unchanged snapshots not re-sent), terminal event when `completed and not running`, `text/event-stream` media type. No test consumes either stream; a framing regression breaks the live GUI progress silently.
**Test:** `test_ingest_stream_sse_format_and_termination` — file: `tests/test_web_ingest.py`
```python
def test_ingest_stream_sse_format_and_termination(client, env):
    _make_cpmove(env); client.post("/api/ingest/scan", json={})
    with client.stream("GET", "/api/ingest/stream") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = [e for e in resp.iter_text() if e.strip()]
    assert all(e.startswith("data: ") and e.endswith("\n\n") for e in events)
    last = json.loads(events[-1][len("data: "):])
    assert last["completed"] is True and last["running"] is False
```

#### M7. `db prune-oplog` CLI wrapper untested
`db_cmd.py:182-194` exists; only the core `lc.prune_oplog` is tested (`tests/test_db_lifecycle.py:141-163`). Wrapper flag `--days`, JSON output, and exit code are unpinned (also `--json` default off for `history` vs on here — easy to get wrong).
**Test:** `test_cli_db_prune_oplog` — file: `tests/test_db_lifecycle.py`
```python
def test_cli_db_prune_oplog(env, backends):
    _insert_op_aged(days=40); _insert_op_aged(days=1)
    r = CliRunner().invoke(cli, ["--db", str(env / "test.db"), "db", "prune-oplog", "--days", "30", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["pruned"] == 1
    assert Path(json.loads(r.output)["file"]).exists()
    assert execute("SELECT COUNT(*) FROM op_log WHERE completed_at >= datetime('now','-30 days')").fetchone()[0] == 1
```

#### M8. Re-ingest after a FAILED archive — op-row reuse + version inflation unpinned
`op_log.append` (op_log.py:16-20) silently returns the existing row for a repeated idempotency key. After a failed ingest, a re-run re-uploads every member as **new versions** (`next_version_seq` counts prior rows). No test covers: one op_log row per archive across attempts, failed→committed transition, and version numbering on retry. A regression here could double-ingest or (if a UNIQUE constraint is ever added to `idempotency_key`) turn every retry into a permanent failure.
**Test:** `test_failed_archive_rerun_reuses_op_row_and_reingests` — file: `tests/test_cpanel_ingest.py`
```python
def test_failed_archive_rerun_reuses_op_row_and_reingests(drop_root, env, backends, ingest_cfg, monkeypatch):
    monkeypatch.setattr(cpanel_ingest, "upload_bundle", boom)          # first attempt fails
    assert cpanel_ingest.ingest_tree(...)["results"][0]["status"] == "failed"
    monkeypatch.undo()
    out = cpanel_ingest.ingest_tree(...)                                # retry succeeds
    assert out["results"][0]["status"] == "ok"
    assert execute("SELECT COUNT(*) FROM op_log WHERE op_type='cpanel_ingest'").fetchone()[0] == 1
    assert execute("SELECT status FROM op_log WHERE op_type='cpanel_ingest'").fetchone()[0] == "committed"
```

### LOW

#### L1. Empty archive and 1-byte archive
A valid gzip with zero members → `ok` / `members: 0` / manifest written / moved to `.done`; a 1-byte garbage file → `failed` without crashing (the corrupt test uses a 17-byte blob — close but not the boundary cases).
**Test:** `test_empty_and_tiny_archives` — file: `tests/test_cpanel_ingest.py`
```python
def test_empty_and_tiny_archives(env, backends, ingest_cfg):
    _cp_archive(env, "drop/a/empty.tar.gz", _gzip_empty_tar_bytes())
    out = cpanel_ingest.ingest_tree(str(env / "drop"), backends, ingest_cfg)
    assert out["results"][0]["status"] == "ok" and out["results"][0]["members"] == 0
    _cp_archive(env, "drop/b/one.tar.gz", b"x")            # new drop item
    out2 = cpanel_ingest.ingest_tree(...); assert out2["results"][0]["status"] == "failed"
```

#### L2. Duplicate member names inside one archive
Tar allows repeated names. Today: sequential staging+unlink means no collision, and the second copy becomes `version_seq=2` of the same `version_group`. Worth pinning so a future parallel/pipelined extractor doesn't corrupt staging.
**Test:** `test_duplicate_member_names_become_two_versions` — file: `tests/test_cpanel_ingest.py`
```python
def test_duplicate_member_names_become_two_versions(drop_root, env, backends, ingest_cfg):
    _cp_archive(env, "drop/acct/cpmove-acct.tar.gz", _tar_with_duplicate_member("homedir/a.txt", b"v1", b"v2"))
    out = cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    assert out["status"] == "ok"
    seqs = sorted(r[0] for r in execute("SELECT version_seq FROM files WHERE relative_path='homedir/a.txt'"))
    assert seqs == [1, 2]
```

#### L3. `discover()` prunes dirs named `staging` under the drop root
Dot-dirs and `.done` are tested; the `staging` name filter (`cpanel_ingest.py:104-109`) is not.
**Test:** `test_discover_skips_staging_dirs` — file: `tests/test_cpanel_ingest.py`
```python
def test_discover_skips_staging_dirs(drop_root, env):
    _cp_archive(env, "drop/staging/x/cpmove-x.tar.gz", _build_tar_bytes(with_symlink=False, with_traversal=False))
    assert all("staging" not in i["path"] for i in cpanel_ingest.discover(str(drop_root)))
```

#### L4. `sanitize_member` backslash traversal variant
Only forward-slash traversal (`../evil`, `a/../../etc/passwd`) is tested; the original-name check for `..\\..\\evil.txt` (cpanel_ingest.py:148-150) is the anti-smuggling branch.
**Test:** add one assertion to the existing `test_sanitize_member_rejects_traversal_absolute_and_empty` — file: `tests/test_cpanel_ingest.py`
```python
    assert cpanel_ingest.sanitize_member("..\\..\\evil.txt") is None
    assert cpanel_ingest.sanitize_member("homedir\\..\\..\\evil") is None
```

#### L5. `check_staging_budget` unlimited path (`budget_gb <= 0`)
`tests/test_scheduler_automation.py::test_check_staging_budget` covers over-budget, under-budget and missing dir — not `staging_budget_gb: 0` (unlimited).
**Test:** append to `test_check_staging_budget` — file: `tests/test_scheduler_automation.py`
```python
    cfg.cpanel_ingest.staging_budget_gb = 0          # 0 = unlimited
    assert ci.check_staging_budget(dataclasses.asdict(cfg)) is True   # even with blob.bin present
```

#### L6. `pace()` bandwidth token bucket
`bandwidth_limit_mbps` (Phase 3 pacing) has no test at all. At minimum pin: `mbps=0` returns immediately and doesn't touch state; `mbps>0` sleeps ≤ 1 s and deducts tokens.
**Test:** `test_pace_unlimited_and_throttled` — file: `tests/test_cpanel_ingest.py`
```python
def test_pace_unlimited_and_throttled(monkeypatch):
    cpanel_ingest._PACE_STATE.update({"tokens": 0.0, "last": 0.0})
    t0 = time.monotonic(); cpanel_ingest.pace(); assert time.monotonic() - t0 < 0.05   # mbps=0
    monkeypatched_cfg(bandwidth_limit_mbps=8)                                          # 1 MB/s
    t1 = time.monotonic(); cpanel_ingest.pace(); cpanel_ingest.pace()
    assert time.monotonic() - t1 > 0.2          # throttled
```

#### L7. `_guess_drop_root` fallbacks
Only exercised indirectly (ingest_tree always passes `drop_root`). Direct-call paths: configured drop_root wins; `parent.name == account` → grandparent; plain parent otherwise.
**Test:** `test_guess_drop_root_fallbacks` — file: `tests/test_cpanel_ingest.py` (three direct calls, assert returned base dir; `monkeypatch` a fake `load_config` to avoid touching real config).

#### L8. `/api/ingest/scan` 409 while a scan is running
`test_api_ingest_scan_flow` admits in a comment the worker already finished before the second call. The 409 single-flight branch (routes.py:2443-2445) is unreachable in the current test.
**Test:** set `_ingest_state["running"] = True` via the app's closure state (or monkeypatch `ingest_tree` to block on an Event), then assert `POST /api/ingest/scan` → 409 — file: `tests/test_web_ingest.py`.

---

## 2. Existing tests that assert nothing meaningful or duplicate another

| # | Test (file) | Problem | Fix |
|---|---|---|---|
| W1 | `tests/test_rebalance.py::test_drain_unknown_only_backend_errors` | `assert out["status"] in ("ok", "error")` is a tautology — passes for any status string, even a wrong one. The comment even admits both outcomes are possible. | Split into two exact cases: unknown name with other backends → `ok` with `copied == 0`; unknown name as sole backend → `error` containing "no other healthy backend". |
| W2 | `tests/test_cli_output.py::test_restore_cpanel_missing` | `assert result.exit_code in (0, 1)` accepts both failure and success — pins nothing but "no traceback". | Assert the actual contract (today: exit 0, human line `members restored: 0`; or, if the exit-code fix from M5 lands, exit 1 + error line). |
| W3 | `tests/test_cli_output.py::test_ingest_cpanel_redundancy_flag` | Runs ingest with `--redundancy 2` but only checks exit 0 — never verifies 2 replica rows in `blob_storage`. | After invoke, assert `SELECT COUNT(*) FROM blob_storage WHERE blob_id = ?` >= 2 for the member blob. |
| W4 | `tests/test_cpanel_ingest.py::test_checkpoint_type_and_total` | Near-duplicate: the same checkpoint assertions (type/total/done) already exist inside `test_ingest_tree_end_to_end` (lines 205-208). | Keep one; make the standalone test assert something extra (e.g. checkpoint only written every 25 members + final). |
| W5 | `tests/test_cli_output.py::test_ingest_cpanel_real_archive` vs `test_ingest_cpanel_json_real` | Same scenario built twice, differing only in output format parsing. | Parametrize over `("--json",)` and share one ingest; assert DB rows once. |
| W6 | `tests/test_web_ingest.py::test_api_ingest_scan_flow` (second half) | Second `POST /api/ingest/scan` only checks 200 (comment admits single-flight untested). Worse: it **starts a real background worker** that keeps running after the test — its open SQLite handle later blocks pytest tmp-dir cleanup on Windows (reproduced: `PermissionError [WinError 32] ... test_api_ingest_scan_flow0\isolated.db` during a subsequent session's cleanup). This is a latent flake source on Windows. | Replace with the deterministic 409 test (L8) and ensure the worker joined/completed before fixture teardown (e.g. wait on `running == False` after the last scan, or expose a join hook). |
| W7 | `tests/test_rebalance.py::test_rebalance_limit` | `out["copied"] <= 1` on a 1–2 blob dataset cannot distinguish "limit respected" from "nothing to copy". | Seed ≥ 3 top-level blobs, assert `copied == 1` and `remaining_blobs == total - 1`, then a second unlimited run drains the rest. |

---

## 3. Product observations surfaced by the analysis (no code changed — follow-ups for the owners)

1. **No skip counters in ingest results.** `ingest_archive` tracks `excluded` and oversize skips but the return payload exposes only `members/bytes/dupe/errors`; the manifest records the mismatch only implicitly (`indexed` vs `estimated`). Recommend adding `excluded` / `skipped_oversize` to both the result dict and the generation manifest (H2/M2 tests would then assert on them).
2. **`restore_cpanel` never returns a non-ok status** for unknown account/generation — CLI exit code 0 on a failed restore (M5).
3. **`POST /api/ingest/scan` clears the pause flag** (routes.py:2446) — resuming by side effect. Either document in the UI or return 409 when paused.
4. **Dead code/fields:** `ingest_cmd.py:153-159` (post-`--watch` loop lines unreachable when `--watch` is set) and `_ingest_state["paused"]` (routes.py:2299, never written — status reads the Event directly).
5. **Windows flake:** web ingest worker thread can outlive a test and hold `isolated.db` open (see W6) — worth a `conftest` guard that joins `_ingest_state` workers before teardown.

---

## 4. Gap counts

| Severity | Count |
|---|---|
| High | 4 (H1 encrypted ingest e2e, H2 max_member_size skip, H3 pause defers, H4 apply_retention) |
| Medium | 8 (M1 account_pattern, M2 exclude_members, M3 raw_backend routing, M4 --watch, M5 restore edges, M6 SSE streams, M7 prune-oplog CLI, M8 failed-archive retry) |
| Low | 8 (L1 empty/tiny archives, L2 duplicate member names, L3 staging-dir pruning, L4 backslash traversal, L5 budget=0, L6 pace(), L7 _guess_drop_root, L8 scan 409) |
| Weak/duplicate existing tests | 7 (W1–W7) |

**Top 3 recommended first:** H1 (encrypted ingest e2e), H2 (max_member_size skip visibility), H3 (pause actually defers) — each is cheap to write against existing fixtures (`enc_env`, `drop_root`, `client`) and each guards a silent failure mode.
