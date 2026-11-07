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
- **Phase 3 re-ingest authorized in principle** — the 20 Softaculous .tar.gz archives on
  `k backupme-data` (stored as whole blobs, 1.0% saved) will be re-processed through the
  extractor once Phase 1 lands; the whole-blob rows get unref'd only after members
  verify. Actual deletion of displaced blobs still goes through the normal gc/purge
  approval gates.
