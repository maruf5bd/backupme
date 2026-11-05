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
