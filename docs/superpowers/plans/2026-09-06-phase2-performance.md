# Phase 2: Performance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backup runs become proportional to change: unchanged re-runs read zero file contents, uploads are verified by remote hash instead of re-download, small files are read once, and the scheduler feeds batches.

**Architecture:** No schema changes, no new modules. `scheduler.py` gains `collect_changes()` and batch-feeding; `uploader.py`'s `_do_upload` switches to hash-based verification with download fallback; the bundle path reads each file once. Spec: `docs/superpowers/specs/2026-09-06-phase2-performance-design.md`.

**Tech Stack:** Python 3.10+, pytest (existing infra from phase 1).

## Global Constraints

- Phase-1 invariants must hold: `reconcile_refcounts() == 0` after runs; DB rows only after successful upload; full round-trip suite stays green.
- Verification is never skipped — only made cheaper (hash-based, download fallback retained).
- All commands run from repo root; `python -m pytest tests/... -v`.
- Same commit style as phase 1.

---

### Task 1: Incremental walk (`collect_changes`)

**Files:**
- Modify: `backupme/scheduler.py`
- Test: `tests/test_incremental.py`

**Interfaces:**
- Produces: `collect_changes(source_root: str, excludes: list[str], force: bool = False, no_skip: bool = False) -> tuple[list[Path], int]` — (candidate Paths relative to nothing — absolute paths — , skipped count). Used by `_run_backups` (Task 2) and `backup_cmd.py` (Task 5).

- [ ] **Step 1: Write failing tests**

```python
import os
import time

import pytest

from backupme.scheduler import collect_changes
from backupme.core.database import execute, commit
from tests.helpers import backup_bytes


def test_second_run_skips_unchanged(env, backends):
    backup_bytes(env, "a.txt", b"stable " * 100, backends)
    candidates, skipped = collect_changes(str(env), [])
    assert candidates == [] and skipped == 1


def test_touched_mtime_reuploads(env, backends):
    backup_bytes(env, "a.txt", b"stable " * 100, backends)
    p = env / "a.txt"
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))
    candidates, skipped = collect_changes(str(env), [])
    assert [c.name for c in candidates] == ["a.txt"] and skipped == 0


def test_size_change_reuploads(env, backends):
    backup_bytes(env, "a.txt", b"stable " * 100, backends)
    (env / "a.txt").write_bytes(b"changed!")
    candidates, _ = collect_changes(str(env), [])
    assert [c.name for c in candidates] == ["a.txt"]


def test_new_file_is_candidate(env, backends):
    backup_bytes(env, "a.txt", b"stable " * 100, backends)
    (env / "new.txt").write_bytes(b"fresh")
    candidates, _ = collect_changes(str(env), [])
    assert sorted(c.name for c in candidates) == ["new.txt"]


def test_legacy_row_without_chunks_not_skipped(env, backends):
    # Indexed row matching size/mtime but with no chunk rows (legacy phantom) — never skip.
    p = env / "phantom.txt"
    data = b"phantom " * 50
    p.write_bytes(data)
    st = p.stat()
    execute(
        """INSERT INTO files (source_root, relative_path, sha256, size, mtime, version_group)
           VALUES (?, 'phantom.txt', ?, ?, ?, 'g')""",
        (str(env), "a" * 64, st.st_size, st.st_mtime),
    )
    commit()
    candidates, skipped = collect_changes(str(env), [])
    assert [c.name for c in candidates] == ["phantom.txt"]


def test_force_returns_everything(env, backends):
    backup_bytes(env, "a.txt", b"stable " * 100, backends)
    candidates, skipped = collect_changes(str(env), [], force=True)
    assert [c.name for c in candidates] == ["a.txt"] and skipped == 0


def test_skip_rules_still_apply(env):
    (env / "junk.log").write_bytes(b"x" * 10)
    candidates, _ = collect_changes(str(env), [])
    assert all(c.name != "junk.log" for c in candidates)
```

Run: `python -m pytest tests/test_incremental.py -v`
Expected: FAIL — `collect_changes` does not exist.

- [ ] **Step 2: Implement in `backupme/scheduler.py`** (add above `apply_retention`)

```python
def collect_changes(
    source_root: str,
    excludes: list[str],
    force: bool = False,
    no_skip: bool = False,
) -> tuple[list, int]:
    """Walk a source directory and split files into backup candidates vs unchanged.

    A file is skipped only when the index has a live, fully-backed-up row
    (chunk rows exist) for the same relative path with identical size and mtime.
    """
    import fnmatch

    from backupme.core.skip import should_skip, should_skip_dir

    src_path = Path(source_root)
    rows = execute(
        """SELECT f.relative_path, f.size, f.mtime,
                  EXISTS(SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id) AS has_chunks
           FROM files f
           WHERE f.source_root = ? AND f.deleted_at IS NULL
             AND f.id = (SELECT MAX(g.id) FROM files g
                         WHERE g.source_root = f.source_root
                           AND g.relative_path = f.relative_path
                           AND g.deleted_at IS NULL)""",
        (str(src_path),),
    ).fetchall()
    known = {os.path.normpath(r["relative_path"]): r for r in rows}

    candidates: list[Path] = []
    skipped = 0

    def consider(fp: Path, rel: str) -> None:
        nonlocal skipped
        if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
            return
        if not no_skip:
            skip, _ = should_skip(str(fp))
            if skip:
                return
        if not force:
            entry = known.get(os.path.normpath(rel))
            if entry is not None and entry["has_chunks"]:
                st = fp.stat()
                if entry["size"] == st.st_size and entry["mtime"] == st.st_mtime:
                    skipped += 1
                    return
        candidates.append(fp)

    if src_path.is_file():
        consider(src_path, src_path.name)
        return candidates, skipped

    for dirpath, dirnames, filenames in os.walk(str(src_path)):
        if not no_skip:
            dirnames[:] = [d for d in dirnames if not should_skip_dir(os.path.join(dirpath, d))]
        for fname in filenames:
            fp = Path(dirpath) / fname
            consider(fp, os.path.relpath(str(fp), str(src_path)))
    return candidates, skipped
```

(`os`, `Path`, `execute` are already imported in scheduler.py.)

- [ ] **Step 3: Run tests green + full suite**

Run: `python -m pytest tests/test_incremental.py -v` → 7 passed. `python -m pytest` → all green.

- [ ] **Step 4: Commit**

```bash
git add backupme/scheduler.py tests/test_incremental.py
git commit -m "feat(scheduler): index-based incremental walk (collect_changes)"
```

---

### Task 2: Batch feeding + incremental in the scheduler loop

**Files:**
- Modify: `backupme/scheduler.py` (`_run_backups`)
- Test: `tests/test_batch_feed.py`

**Interfaces:**
- `_run_backups` now: per backup entry → `collect_changes` → feeds candidates to `uploader.upload_bundle` in batches (200 files / 64 MB) → handles `move` per result. No per-file `upload_file` calls remain.

- [ ] **Step 1: Write failing tests**

```python
import dataclasses

import pytest

from backupme.core.database import execute
from backupme.core import dedup, uploader as uploader_mod
from tests.helpers import load_cfg


def _scheduler_config(env):
    cfg = dataclasses.asdict(load_cfg())
    cfg["backups"] = [{"source": str(env)}]
    return cfg


def test_run_backups_uses_one_bundle_for_many_small_files(env, backends):
    from backupme.scheduler import BackupScheduler
    for i in range(300):
        (env / f"s{i:03}.txt").write_bytes(b"batch " * 20)
    sched = BackupScheduler(_scheduler_config(env))
    sched._run_backups()
    bundles = execute("SELECT COUNT(*) FROM blobs WHERE blob_id LIKE 'bundle/%' AND blob_id NOT LIKE 'bundle/%/%/%'").fetchone()[0]
    # 300 tiny files must share bundles (<= 3 bundle blobs), not 300
    assert bundles <= 3
    assert dedup.reconcile_refcounts() == 0


def test_run_backups_second_run_is_noop(env, backends):
    from backupme.scheduler import BackupScheduler
    (env / "once.txt").write_bytes(b"once " * 100)
    sched = BackupScheduler(_scheduler_config(env))
    sched._run_backups()
    files_before = execute("SELECT COUNT(*) FROM files").fetchone()[0]
    results_seen = []
    real = uploader_mod.upload_bundle
    import backupme.scheduler as sched_mod

    def spy(infos, *a, **kw):
        results_seen.append(list(infos))
        return real(infos, *a, **kw)

    sched_mod.uploader.upload_bundle = spy
    try:
        sched._run_backups()
    finally:
        sched_mod.uploader.upload_bundle = real
    assert results_seen == [] or all(len(b) == 0 for b in results_seen)
    assert execute("SELECT COUNT(*) FROM files").fetchone()[0] == files_before


def test_run_backups_survives_batch_failure(env, backends, monkeypatch):
    from backupme.scheduler import BackupScheduler
    for i in range(5):
        (env / f"f{i}.txt").write_bytes(b"data " * 10)
    sched = BackupScheduler(_scheduler_config(env))
    monkeypatch.setattr(uploader_mod, "upload_bundle",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    sched._run_backups()  # must not raise
```

Run: `python -m pytest tests/test_batch_feed.py -v`
Expected: FAIL — scheduler still calls `upload_file` per file (bundle count ~300, no batch behavior).

- [ ] **Step 2: Replace `_run_backups` in `backupme/scheduler.py`**

```python
    def _run_backups(self):
        from backupme.core.uploader import upload_bundle

        for entry in self.config.get("backups", []):
            source = entry.get("source", "")
            target = entry.get("target", "")
            redundancy = entry.get("redundancy", 1)

            if not source or not os.path.exists(source):
                continue

            from backupme.backends import get_backend

            backends = get_all_backends(self.config)
            if target:
                backends = [b for b in backends if b.name == target]
            if not backends:
                continue

            src_path = Path(source)
            if src_path.is_file():
                skip, _ = should_skip(str(src_path))
                if skip:
                    continue
                candidates = [src_path]
                skipped = 0
            else:
                candidates, skipped = collect_changes(
                    str(src_path), entry.get("exclude", []))

            if skipped:
                print(f"{source}: {skipped} unchanged (skipped)")

            batch: list[dict] = []
            batch_bytes = 0

            def flush() -> None:
                nonlocal batch, batch_bytes
                if not batch:
                    return
                try:
                    results = upload_bundle(batch, backends, self.config, redundancy)
                    for r in results:
                        if entry.get("move") and r.get("file_id") and r.get("local_path"):
                            try:
                                os.remove(r["local_path"])
                            except OSError:
                                pass
                except Exception:
                    import traceback
                    traceback.print_exc()
                batch.clear()
                batch_bytes = 0

            for fp in candidates:
                st = fp.stat()
                batch.append({
                    "local_path": str(fp),
                    "source_root": str(src_path),
                    "relative_path": os.path.relpath(str(fp), str(src_path)),
                })
                batch_bytes += st.st_size
                if len(batch) >= 200 or batch_bytes >= 64 * 1024 * 1024:
                    flush()
            flush()
```

- [ ] **Step 3: Run tests green + full suite**

Run: `python -m pytest tests/test_batch_feed.py -v` → 3 passed. `python -m pytest` → all green.

- [ ] **Step 4: Commit**

```bash
git add backupme/scheduler.py tests/test_batch_feed.py
git commit -m "feat(scheduler): feed batches to the bundler; incremental in the daemon loop"
```

---

### Task 3: Hash-based upload verification (P2.2)

**Files:**
- Modify: `backupme/core/uploader.py` (`_do_upload` + its 3 call sites), `docs/superpowers/specs/2026-09-06-phase2-performance-design.md` (step-4 wording)
- Test: `tests/test_upload_verify.py`

**Interfaces:**
- `_do_upload(backend, local_path: str, remote_path: str, expected_sha256: str) -> bool` — callers pass the sha256 they already computed for the bytes being uploaded. Manual sleeps are removed (the `ExponentialBackoff` iterator already sleeps between attempts).

- [ ] **Step 1: Write failing tests**

```python
import hashlib

import pytest

from backupme.backends.local import LocalBackend
from backupme.core.uploader import _do_upload


class VerifySpyBackend(LocalBackend):
    """Counts hash()/download() calls; can lie about hashes or corrupt uploads."""

    def __init__(self, name, config, hash_mode="correct", corrupt=False):
        super().__init__(name, config)
        self.hash_mode = hash_mode
        self.corrupt = corrupt
        self.hash_calls = 0
        self.download_calls = 0

    def hash(self, remote_rel_path: str) -> str:
        self.hash_calls += 1
        if self.hash_mode == "raise":
            raise RuntimeError("no remote hash for you")
        real = super().hash(remote_rel_path)
        if self.hash_mode == "lie":
            return "0" * 64
        return real

    def download(self, remote_rel_path: str, local_path: str):
        self.download_calls += 1
        return super().download(remote_rel_path, local_path)

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        if self.corrupt:
            import shutil
            shutil.copyfile(local_path, local_path + ".probe")
            with open(local_path + ".probe", "r+b") as f:
                f.seek(0)
                f.write(b"CORRUPTED!")
            out = super().upload(local_path + ".probe", remote_rel_path)
            import os
            os.remove(local_path + ".probe")
            return out
        return super().upload(local_path, remote_rel_path)


def _src(tmp_path):
    p = tmp_path / "data.bin"
    p.write_bytes(b"verify me " * 500)
    return str(p), hashlib.sha256(p.read_bytes()).hexdigest()


def test_correct_hash_never_downloads(tmp_path):
    bk = VerifySpyBackend("v1", {"path": str(tmp_path / "store")})
    bk.connect()
    src, sha = _src(tmp_path)
    assert _do_upload(bk, src, "objects/aa/bb/x", sha) is True
    assert bk.hash_calls >= 1 and bk.download_calls == 0


def test_raising_hash_falls_back_to_download(tmp_path):
    bk = VerifySpyBackend("v2", {"path": str(tmp_path / "store")}, hash_mode="raise")
    bk.connect()
    src, sha = _src(tmp_path)
    assert _do_upload(bk, src, "objects/aa/bb/x", sha) is True
    assert bk.download_calls >= 1


def test_lying_hash_caught_by_download(tmp_path):
    bk = VerifySpyBackend("v3", {"path": str(tmp_path / "store")}, hash_mode="lie")
    bk.connect()
    src, sha = _src(tmp_path)
    assert _do_upload(bk, src, "objects/aa/bb/x", sha) is True
    assert bk.download_calls >= 1


def test_corrupt_upload_fails_after_retries(tmp_path):
    bk = VerifySpyBackend("v4", {"path": str(tmp_path / "store")}, hash_mode="raise", corrupt=True)
    bk.connect()
    src, sha = _src(tmp_path)
    assert _do_upload(bk, src, "objects/aa/bb/x", sha) is False
```

Run: `python -m pytest tests/test_upload_verify.py -v`
Expected: FAIL — `_do_upload` takes 3 args and re-computes its own hash (signature TypeError).

- [ ] **Step 2: Replace `_do_upload` in `backupme/core/uploader.py`**

```python
def _do_upload(backend, local_path: str, remote_path: str, expected_sha256: str) -> bool:
    retry = retry_mod.ExponentialBackoff()
    for attempt, delay in retry:
        try:
            backend.upload(local_path, remote_path)

            # Prefer the backend's remote hash: no data transfer needed.
            remote_hash = None
            try:
                remote_hash = backend.hash(remote_path)
            except Exception:
                remote_hash = None

            if remote_hash and remote_hash.lower() == expected_sha256.lower():
                _circuit_breaker.record_success(backend.name)
                return True

            # Hash unavailable (or lied) — download-back verification,
            # including the 0-byte stub check for async virtual filesystems.
            dl_dir = Path(tempfile.mkdtemp())
            try:
                dl = dl_dir / "verify.tmp"
                backend.download(remote_path, str(dl))
                if not dl.exists():
                    raise RuntimeError("Downloaded file does not exist after verification download")
                if dl.stat().st_size == 0 and os.path.getsize(local_path) > 0:
                    raise RuntimeError("Downloaded file is empty (detected broken virtual stub)")
                remote_hash = hasher_mod.sha256_file(str(dl))
            finally:
                import shutil
                shutil.rmtree(str(dl_dir), ignore_errors=True)

            if remote_hash.lower() == expected_sha256.lower():
                _circuit_breaker.record_success(backend.name)
                return True
        except Exception:
            pass
    _circuit_breaker.record_failure(backend.name)
    return False
```

Update the three call sites to pass the already-computed hash:
- `_upload_chunk_to_backends`: `_do_upload(bk, str(tmp), remote_path, stored_sha256)`
- `_upload_single_blob` (encrypt_file branch): `_do_upload(bk, str(enc_file), remote_path, stored_sha256)`
- bundle `_flush_batch`: `_do_upload(bk, str(tmp), remote_path, stored_sha256)`

Also update the spec's P2.2 step 4 wording to: "A hash mismatch or unavailable hash falls back to download-back verification on that attempt (stronger than the original final-attempt-only guard, same cost profile)."

```bash
git add docs/superpowers/specs/2026-09-06-phase2-performance-design.md
```

- [ ] **Step 3: Run tests green + full suite**

Run: `python -m pytest tests/test_upload_verify.py -v` → 4 passed. `python -m pytest` → all green.

- [ ] **Step 4: Commit**

```bash
git add backupme/core/uploader.py tests/test_upload_verify.py docs/superpowers/specs/2026-09-06-phase2-performance-design.md
git commit -m "perf(uploader): verify uploads via backend hash; download-back only as fallback"
```

---

### Task 4: Single-pass reads for the bundle path (P2.3)

**Files:**
- Modify: `backupme/core/uploader.py` (`upload_bundle` main-loop prologue)
- Test: `tests/test_single_pass.py`

- [ ] **Step 1: Write failing test**

```python
import builtins

import pytest

import backupme.core.uploader as uploader_mod
from tests.helpers import load_cfg
from backupme.core.uploader import upload_bundle


def test_small_file_read_once(env, backends, monkeypatch):
    p = env / "once.bin"
    p.write_bytes(b"read once " * 100)
    opens = []
    real_open = builtins.open

    def counting_open(file, *a, **kw):
        if "once.bin" in str(file):
            opens.append(str(file))
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", counting_open)
    results = upload_bundle(
        [{"local_path": str(p), "source_root": str(env), "relative_path": "once.bin"}],
        backends, load_cfg(), 1,
    )
    assert results[0]["status"] == "ok"
    assert len(opens) == 1, opens
```

Run: `python -m pytest tests/test_single_pass.py -v`
Expected: FAIL — currently 2 opens (hash pass + read).

- [ ] **Step 2: Edit the `upload_bundle` main-loop prologue**

Replace:

```python
            file_size = os.path.getsize(src_path)
            file_sha256 = hasher_mod.sha256_file(src_path)
            mtime = os.path.getmtime(src_path)
```

with:

```python
            st = os.stat(src_path)
            file_size = st.st_size
            mtime = st.st_mtime

            # Read small files once: hash the buffer (chunked files still need
            # a separate hash pass because dedup must be decided before upload).
            data = Path(src_path).read_bytes() if file_size <= _vault_max else None
            file_sha256 = (
                hashlib.sha256(data).hexdigest() if data is not None
                else hasher_mod.sha256_file(src_path)
            )
```

and in the bundle branch, replace `data = Path(src_path).read_bytes()` with `# data already read above` (the `data` variable is already bound; the `batch.append((item_info, manifest, data))` line stays).

Note: the `elif file_size <= _vault_max:` branch must keep its `data` reference — the code above binds `data` for small files and `None` for large ones; the chunked branch (`else`) does not use `data`.

- [ ] **Step 3: Run green + full suite**

Run: `python -m pytest tests/test_single_pass.py -v` → 1 passed; `python -m pytest` → all green.

- [ ] **Step 4: Commit**

```bash
git add backupme/core/uploader.py tests/test_single_pass.py
git commit -m "perf(uploader): single read pass for files <= 10MB"
```

---

### Task 5: Config wiring + micro-fixes (P2.5)

**Files:**
- Modify: `backupme/core/uploader.py` (chunk_size wiring), `backupme/core/skip.py` (resolve only when needed), `backupme/cli/backup_cmd.py` (`--force` + incremental)
- Test: `tests/test_chunk_config.py`, `tests/test_skip.py` (add one test)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_chunk_config.py
from backupme.core.database import execute
from backupme.core.uploader import upload_bundle
from tests.helpers import load_cfg


def test_chunk_size_config_respected(env, backends):
    p = env / "three.mb"
    p.write_bytes(bytes(range(256)) * (3 * 1024 * 1024 // 256))
    cfg = load_cfg()
    d = __import__("dataclasses").asdict(cfg)
    d["chunking"]["chunk_size"] = 1024 * 1024
    results = upload_bundle(
        [{"local_path": str(p), "source_root": str(env), "relative_path": "three.mb"}],
        backends, d, 1,
    )
    chunks = execute(
        "SELECT COUNT(*) FROM file_chunks WHERE file_id = ?", (results[0]["file_id"],)
    ).fetchone()[0]
    assert chunks == 3
```

```python
# append to tests/test_skip.py
def test_absolute_path_skips_resolve(monkeypatch):
    from backupme.core import skip as skip_mod

    def boom(*a, **kw):
        raise AssertionError("resolve() called for absolute path")

    monkeypatch.setattr(skip_mod.Path, "resolve", boom)
    skip, why = skip_mod.should_skip(r"C:\logs\app.log")
    assert skip
```

Run: `python -m pytest tests/test_chunk_config.py tests/test_skip.py -v`
Expected: chunk test FAILS (3 MB file → 1 chunk, hardcoded 64 MB); resolve test FAILS (`resolve()` is called unconditionally).

- [ ] **Step 2: Wire chunk_size in `backupme/core/uploader.py`**

At the top of `upload_bundle`, after the `config = asdict(config)` normalization:

```python
    chunk_cfg = config.get("chunking", {})
    raw_chunk = chunk_cfg.get("chunk_size", _chunk_size)
    if isinstance(raw_chunk, str):
        import re as _re
        m = _re.match(r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?\s*$", raw_chunk, _re.IGNORECASE)
        chunk_size = int(float(m.group(1)) * {"B": 1, "KB": 1024, "MB": 1024**2,
                                              "GB": 1024**3, "TB": 1024**4}.get((m.group(2) or "B").upper(), 1)) if m else _chunk_size
    else:
        chunk_size = int(raw_chunk)
```

and replace the two uses of `_chunk_size` inside the loop (`range(0, file_size, _chunk_size)` and `fsrc.read(_chunk_size)`) with `chunk_size`.

- [ ] **Step 3: `skip.py` — resolve only non-absolute paths**

In `should_skip`, replace:

```python
    parts = p.resolve().parts
    full_path = str(p.resolve())
```

with:

```python
    if p.is_absolute():
        rp = p
    else:
        rp = p.resolve()
    parts = rp.parts
    full_path = str(rp)
```

Same change in `should_skip_dir` (`full = str(p.resolve())` → conditional).

- [ ] **Step 4: CLI `backup` gains `--force` and incremental (P2.1 applied to the CLI)**

In `backup_cmd.py`: add option `@click.option("--force", is_flag=True, help="Re-offer every file, skipping the unchanged check")`, then replace the directory-walk block:

```python
        elif os.path.isdir(path) and recursive:
            from ..scheduler import collect_changes
            candidates, skipped = collect_changes(path, [], force=force, no_skip=no_skip)
            if skipped:
                _safe_echo(f"{path}: {skipped} unchanged (skipped)")
            for fp in candidates:
                rel = os.path.relpath(str(fp), path)
                files_to_backup.append({
                    "local_path": str(fp),
                    "source_root": path,
                    "relative_path": rel,
                })
```

(The old inline walk with per-file `should_skip` is removed — `collect_changes` applies the same rules.)

- [ ] **Step 5: Run green + full suite**

Run: `python -m pytest tests/test_chunk_config.py tests/test_skip.py tests/test_incremental.py -v` → all passed; `python -m pytest` → all green.

- [ ] **Step 6: Commit**

```bash
git add backupme/core/uploader.py backupme/core/skip.py backupme/cli/backup_cmd.py tests/test_chunk_config.py tests/test_skip.py
git commit -m "perf: wire chunk_size config, skip resolve for absolute paths, CLI --force + incremental"
```

---

### Task 6: Final verification

- [ ] **Step 1: Full suite** — `python -m pytest` → all green.

- [ ] **Step 2: Manual performance smoke with the CLI**

```bash
python main.py -c test_tmp_backend/roundtrip/config.yaml --db-path test_tmp_backend/roundtrip/rt3.db backup test_data -r -b roundtrip_local
python main.py -c test_tmp_backend/roundtrip/config.yaml --db-path test_tmp_backend/roundtrip/rt3.db backup test_data -r -b roundtrip_local
```

Expected: first run uploads 3 files; second run prints `test_data: 3 unchanged (skipped)` and no upload lines. Then `report verify` on rt3.db reports 0 failed.

- [ ] **Step 3: Commit (if anything pending) and merge-ready report**

```bash
git add -A
git commit -m "chore: phase 2 verification"  # only if there are changes
```

## Self-Review Notes

- Spec coverage: P2.1 → Tasks 1, 2, 5; P2.2 → Task 3; P2.3 → Task 4; P2.4 → Task 2; P2.5 → Task 5. Success bar mapped to tests in each task plus Task 6 smoke.
- Type consistency: `collect_changes` signature identical in Tasks 1, 2, 5; `_do_upload` 4-arg signature used by all three call sites in Task 3.
- Intentional deviation from spec P2.2: mismatch/missing hash falls back to download-verify on *every* affected attempt rather than only the final one (simpler, stronger, same cost profile on healthy backends). Spec updated in Task 3.
