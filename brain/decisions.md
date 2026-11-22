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

## 2026-11-12

- **hostraj Gdrive root repointed + Storage Box fill (owner-approved, executed)** — the
  configured root moved from the near-empty `H:\Backupme` to the real store
  `H:\New folder\backupme` (D14); storagebox config path `/` -> `backupme` subdir; the
  phase-1 rclone copy landed 86.5 GiB on the box (64.2 GiB New-folder store + 22.0
  local_disk + 0.3 K) in ~10 h at the ~3.17 MB/s uplink cap. Entry backfilled 2026-11-18 —
  the decision had been recorded only in status.md update 18.
- **All future Storage Box work goes through rclone** — paramiko parallel sessions are
  refused after storms (8 and even 4 workers); the rclone connection pattern ran 10 h immune.

## 2026-11-18

- **Deep check with a 3-way background fan-out (owner-directed)** — the owner explicitly
  raised concurrency to 3 for this run and the harness accepted all three agents. The
  AGENTS.md max-2 default stands for future runs unless the owner says otherwise.
- **Truth-register primacy** — `brain/defects.md` is the authoritative defect register;
  AGENTS.md "Known broken" only summarizes it and must be updated in the same pass whenever
  a defect opens/closes or a root/path changes (today's D14 contradiction fix).
- **Owner approval scope today: brain + AGENTS.md fixes only.** Deferred pending approval:
  finishing + committing the docstring sweep, fixing the 2026-11-18 core-review defects
  (D19-D22) and the P3 hygiene batch, pushing the 3 unpushed commits (5865a9b, d05a64b,
  4a270ee).

## 2026-11-19

- **Reject `H:\New folder\backupme` deletion until Queue 2 finishes and passes physical verification** —
  A catalog match (`hostraj Gdrive` count == `gdrive_maruf` count) is insufficient proof when premature SQL
  cloning occurred. We mandate physical spot-check verification before deleting any local source repository.
- **Enforce 50 MB bundle accumulation for small files** —
  Accumulate small files into single ~50MB binary `.tar.zstd` containers (`_bundle_target_size = 50MB`,
  batching limits 50MB / 1,000 files) to drastically cut per-file latency and round-trips over remote storage.
- **Cap Hetzner Storage Box SFTP concurrency at 8 and chunk size at 32k** —
  Hetzner Storage Box enforces a hard 10-connection limit and standard OpenSSH 32k packet buffer.
  Setting `--sftp-concurrency 8` and `--sftp-chunk-size 32k` prevents EOF disconnects.
- **Tune SFTP concurrency to conservative bounds (<=2) for large bundles over slow uplinks** —
  When uploading ~50MB bundles over uplinks where transfer duration approaches 100 seconds, high concurrency
  delays packet ACKs and causes EOF disconnects. Concurrency must be throttled to 1-2 to ensure timely packet
  flow and prevent channel termination.

## 2026-11-21

- **"continue" = land the update-41 handoff state.** The owner's instruction to continue
  was read as approval to commit the tree that the Nov-20 full gate had already proven
  (971 collected / 1 pre-existing failure). Both streams' work went into one commit
  `e02b0ac` (hunk-level separation impractical), then the brain/docs wave as its own
  commit. This also serves D23's "land the J WIP" fix direction: production executes
  the J working tree, so an uncommitted tree was production risk.
- **Push of the new commits stays gated** — outward-facing actions still need the
  owner's explicit go, per the standing rule.
- **D30 and the D33 serial-loop rework need the owner's design sign-off** before any
  restructuring (recorded in update 41 and unchanged).

## 2026-11-22 (production-readiness mandate)

- **Owner mandate: "you are my serious dev making the app production ready — do
  whatever needs to be done."** Read as approval for: pushing committed work to origin
  (CI gates it), implementing the two deferred structural reworks (D30 backend resolver,
  D33-SCH-2 parallel ticks) with the normal gate discipline, release engineering
  (CHANGELOG + version + tag), and restarting the dashboard on released code.
- **D23 recommendation recorded (owner still owns the final call on migrating K):**
  J's git repo is the code source of truth; production executes its committed tree;
  K:\backupme remains the production DATA home. Do not treat K as a code workspace.
- **The D23 hazard rule stays**: destructive gates (gc --apply, purges) must never be
  driven against K's database without re-verified numbers.
