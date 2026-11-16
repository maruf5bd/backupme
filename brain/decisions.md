# Decision log (append-only)

## 2026-11-05

- **Language: English everywhere** — chat, commits, code, docs, reports, subagent prompts.
  Enforced via AGENTS.md.
- **Fix-first rule** — broken things get fixed, never skipped/silenced/worked around; anything
  genuinely unfixable in-session is registered in AGENTS.md "Known broken" with root cause +
  unblock condition. No ignoring allowed.
- **Subagent discipline** — max 2 concurrent background agents; exactly one DB-writer at a
  time; disjoint file ownership per agent; separate pytest basetemps when two agents run
  tests concurrently.
- **B02: remove dead config keys, do not re-implement** — `chunking.parallel_uploads` and
  `verification.fallback_on_fail` are deleted from config.yaml; warnings stop with zero
  behavior change. Parallel uploads can return later as a properly specced P2 feature.
- **Purge candidates definition** (data-health module docstring): live files with ZERO chunks,
  or ALL chunks' blobs missing from blob_storage. Files with only SOME missing blobs appear in
  the `missing_blob` bucket but are NOT purge candidates (they may still restore partially or
  be re-backable).
- **Purge safety** — every purge requires explicit user approval (dashboard confirm dialog or
  chat), soft-delete only via `_soft_delete_file()`, re-verification at purge time, op_log
  audit entry (B12). 2FA.7z is the last copy in existence — extra guard.
- **Bundle-member reachability rule** — a member blob is reachable iff its PARENT bundle file
  exists and the member's plaintext byte range (offset+len) fits the parent's PLAINTEXT size.
  Never compare against compressed on-disk size (that was D2). Decompress-hash is the
  definitive content check for borderline cases.
- **Consolidation target** — all blob copies live in one place: `H:\Backupme` (Gdrive backend
  root). Old roots `E:\hostraj\backupme` and `H:\New folder\backupme` are not sources of truth;
  never copy from them without decompress-hash verification (24 corrupt files live there).
- **Gdrive "loss" corrected** — the 51,644 "lost rows" estimate was wrong; true loss is exactly
  1 bundle (2 blobs) affecting 1 live file. Lesson recorded in lessons.md.

## 2026-11-05 (update 2)

- **GC apply gate** — remote-blob deletion (`gc --apply`) runs only after a three-step
  cross-verification of the dry-run: (1) matcher health — the count of matched files must
  equal the count of live DB rows for that backend; (2) category sweep — every orphan path
  checked against `blobs`, `file_chunks`, and `blob_storage`; (3) zero-reference proof —
  any orphan with even one reference row is excluded from deletion. Applied to the
  2026-11-05 finding (8,498 orphans / 28.22 GiB): all category-D, zero rows anywhere;
  deletion awaits explicit user approval.
- **Gdrive GC coverage deferred** — the `hostraj Gdrive` backend was unreachable during the
  2026-11-05 scan, so its orphan picture is unknown; GC must be re-run there once the root
  is mounted (old-generation junk likely exists there too).

## 2026-11-08

- **Extract-first archives APPROVED (owner)** — archives are extracted on backup, every
  member hashed + uploaded through the real pipeline, the archive lives only as a DB
  manifest (fingerprint = hash of its member list → archive-level dedup on top of file
  and chunk dedup), members are ordinary `files` rows under `archive/<fp>/`, nested
  archives recurse with caps, corrupt/encrypted/unsupported → whole-file fallback.
  Owner's insurance add-on accepted: manifest sidecars WITH chunk recipes written to
  every backend (`manifests/<2>/<2>/<fp>.json`, skipped when encryption is enabled) so
  blobs survive DB loss; plus daily off-site DB snapshot shipping (`guard/snapshots/`,
  `db snapshot --offsite`, scheduler tick; no remote deletes in v1). Full plan with
  phases and open decisions: `brain/plan_archives.md`.
- **Repacked-archive caveat accepted** — restores rebuild archives from DB data with the
  same files/structure but non-identical compression bytes; "keep original too" stays a
  future exception flag, default OFF.
## 2026-11-16

- **GitHub Actions CI adopted as the release gate** — every push runs windows-latest:
  editable install → CLI smoke → wheel build + dashboard-asset assert → full pytest
  (710 passed, 1 skipped, first green run `429e8af`). Rationale: a real release
  pipeline is the professional baseline the pro-app audit named; CI exposed five
  packaging/clean-runner assumptions a local dev box was hiding (see status update 28).
- **`smbclient` rides as an optional `smb` extra** — it publishes no Python 3.13
  distribution on PyPI, so it cannot be a hard dependency; the SMB backend lazy-imports
  it and degrades gracefully when absent. `paramiko` pinned to the tested major (`<5`).
- **`python-multipart` is now a declared hard dependency** — FastAPI's form-upload
  endpoint raises without it on a clean install.
- **`backupme init` is the canonical first-run path** — scaffolds `config.yaml` as a
  byte-copy of `config.example.yaml`, initializes the DB, prints next steps, and never
  overwrites an existing config. First-run no longer silently succeeds on built-in
  defaults while backing up nothing.
- **CLI destructive ops are op_log-audited** (`gc --apply`, `db purge`,
  `prune-phantom-rows --apply`) at the core layer, mirroring the dashboard — so the
  scheduler's purge/gc ticks are covered too.
- **`--help` must never touch disk** — the group callback skips BACKUPME_CONFIG/config/DB
  side effects when only help is requested; `backupme db --help` no longer scatters a
  `backupme.db` in CWD.
- **Test suite must run on a fresh clone** — `tests/conftest.py` creates the pinned
  basetemp parent; a relative basetemp with no parent errors WinError 3 on a clean
  checkout (CI caught it).
