# Plan: extract-first archives (approved 2026-11-08)

Owner-approved design (user + assistant, refined over the 2026-11-07/08 chat).
This file is the single source of truth for the feature. Status markers:
DONE / IN FLIGHT / PENDING.

## Goal (user's words, formalized)

Every archive (zip/tar/rar/…) that BackupMe backs up must be **extracted
first**: each contained file is hashed and stored through the normal dedup
pipeline, the archive itself is recorded in the DB as a manifest of those
hashed files (never stored as a blob), nested archives are processed the same
way and recorded as independent archives, and restores are rebuilt from the
DB. Corrupt/unsupported archives fall back to whole-file storage — data is
never lost.

## Dedup model — three levels

1. **Archive level**: the archive's fingerprint = sha256 over its sorted
   member manifest (lines: `relative_path\0member_sha256\0size\n`, UTF-8).
   Same content twice → same fingerprint → zero new bytes.
2. **File level**: members are real `files` rows under
   `source_root = "archive/<fingerprint>"` (relative_path = path inside the
   archive) — they dedup against the ENTIRE existing index via the normal
   donor lookup.
3. **Chunk level**: unchanged for big members (> chunking.min_size → chunked
   path; small members batched inline) — existing pipeline.

## DB schema (Phase 1)

- `archives(id PK, fingerprint TEXT UNIQUE, format TEXT, original_name TEXT,
  original_size INT, member_count INT, total_uncompressed INT,
  created_at DEFAULT datetime('now'))`
- `archive_members(id PK, archive_id → archives ON DELETE CASCADE,
  relative_path TEXT, file_id → files(id), member_sha256 TEXT, member_size INT,
  mtime REAL, nested_archive TEXT NULL — fingerprint of a nested archive,
  UNIQUE(archive_id, relative_path))`
- Indexes on archives(fingerprint), archive_members(archive_id).

## Extraction rules

- Detection by MAGIC BYTES (not extension): gzip `1f 8b`, zip `PK\x03\x04`,
  tar (ustar at offset 257), xz `fd 37 7a 58 5a 00`, bzip2 `BZh`.
- Phase 1 formats: tar / tar.gz / tgz / tar.bz2 / tar.xz (tarfile) + zip
  (zipfile). Phase 2: 7z (py7zr), rar (unrar/7z external tool if present).
- Member path safety: reuse cpanel_ingest's sanitize rules (reject traversal,
  absolute, backslash tricks) — zip-slip protection.
- Nested archives: a member whose bytes match archive magic is extracted
  recursively; it gets its OWN fingerprint/rows; the parent's member entry
  carries `nested_archive: <inner fingerprint>`. Caps: depth ≤ 5, ≤ 200,000
  members per archive, ≤ 20 GiB total uncompressed per archive (nested
  counts toward the parent budget).
- Fallback (never lose data): corrupt archive, encrypted/password archive,
  unsupported format, extraction error, or any cap exceeded → EXISTING
  whole-blob path, result reports `stored_whole` + reason.
- Staging follows the cpanel_ingest pattern: members extracted to a temp
  dir, fed through `upload_bundle`, staging removed in `finally`.

## Manifest sidecars — DB-loss insurance (user's idea)

- After a successful ingest, write ONE JSON per archive to EVERY target
  backend at `manifests/<fp[:2]>/<fp[2:4]>/<fp>.json` — content-addressed,
  byte-identical across backends, backend-agnostic recipe:
  `format_version, fingerprint, format, original_name, original_size,
  member_count, total_uncompressed, created_utc, members[]` where each member
  has `path, sha256, size, mtime`, either `chunks: [{blob_id, offset,
  length}]` (full restore recipe — works WITHOUT any DB) or
  `nested_archive: <fp>`.
- Encryption rule: when config encryption is enabled, sidecars are SKIPPED
  (plaintext filenames must not leak to backends) — mirrors the content_text
  rule. DB manifest always records everything.
- Purpose: if the DB is corrupted/lost, blobs + manifests alone can identify
  and reassemble every archive's content.

## Off-site DB snapshot shipping (assistant's add-on, same insurance layer)

- The whole DB is the ultimate manifest; today `db snapshot` files never
  leave the working dir. Ship each new snapshot to every backend under
  `guard/snapshots/<UTC ts>-gen<generation>.db.zst`, verify per-backend,
  partial-failure tolerant, scheduler daily tick (single-flight, exception
  guard), CLI `db snapshot --offsite`, new `guard` config section.
- Retention pruning of old remote snapshots deliberately DEFERRED (no
  deletes in v1). ~177 MB/day; box has 1.2 TB free → years of headroom.

## Restore semantics

- Members: restorable TODAY via all existing paths (per-file download,
  web restore, search) because they are ordinary `files` rows.
- `archive restore <fp> --output DIR`: writes every member preserving the
  archive's internal structure, verifying each member sha256.
- `archive restore --repack FILE.tar` (optional): re-packs members from DB
  data. **Byte-layout caveat (accepted)**: a repacked archive has the same
  files/structure but is NOT byte-identical to the original (compression
  bytes differ). Optional "keep original too" flag exists as a future
  exception path — default OFF (it would defeat the space savings).

## Recovery tooling (planned)

- `backupme db recover-from-manifests <backend>` (Phase 2/3): scan
  `manifests/**` + the blob store, rebuild files/chunks/blobs rows into a
  fresh DB, report anything unrecoverable. Complements the existing
  `db snapshot` restore path.

## Phases and status

- **Phase 1 — IN FLIGHT (builder agent, 2026-11-08)**: core/archives.py
  (extract + fingerprint + ingest + manifest build/write), DB tables,
  `backup` wiring (archive hook in the file branch), `backupme archive
  list|show|restore`, tests/test_archives.py (11 scenarios incl. same-archive
  no-op, day-2 near-zero-byte dedup, zip-slip, caps, fallbacks, sidecar
  recipe check, fresh-init migration), MANUAL.md §10 rows.
- **IN FLIGHT (parallel agent, 2026-11-08)**: off-site guard module +
  scheduler tick + `db snapshot --offsite` + `guard` config (files:
  core/offsite_guard.py, scheduler.py, cli/db_cmd.py, config.py,
  tests/test_offsite_guard.py; MANUAL lines reported not edited).
- **Phase 2 — PENDING**: 7z via py7zr, rar via external unrar/7z when
  present, dashboard "Archives" page (list archives, browse members, restore
  buttons), `db recover-from-manifests`, manifest retention pruning design.
- **Phase 3 — PENDING (user-visible win)**: re-process the 20 Softaculous
  .tar.gz archives already stored on `k backupme-data` (2,258,329,053 B
  stored, saved only 1.0% as whole blobs) through the extractor; unref the
  whole-blob rows after members are verified; expected result: a few hundred
  MB instead of 2.25 GB (14 daily WHMCS snapshots share ~99% of files).

## Open decisions (owner)

- keep-original-blob exception flag: default OFF (agreed; revisit if byte-
  exact archive restores become a need).
- Remote snapshot/manifest retention (deletes on backends): deferred until
  owner approves a pruning rule.
- Phase-2 dashboard scope (page layout) to be proposed when Phase 1 lands.
