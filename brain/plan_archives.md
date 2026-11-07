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
- **NAMESPACE NOTE (live-verified 2026-11-08 on K)**: `manifests/` on the
  backends ALREADY carried per-FILE receipts from `core/manifest.py`
  (`<32hex>.manifest.json`, format v2) — those receipts drive the EXISTING
  `rebuild_database_from_manifests` (the `db rebuild` recovery path), so the
  owner's DB-loss insurance existed at file level before this feature. The
  archive sidecars (64-hex names) coexist in the same tree; consumers must
  shape-filter (recover-from-manifests accepts only 64-hex stems with
  `fingerprint`+`members` keys; a receipt-rebuild counts sidecars as
  unreadable noise — harmless). First live run proved the sidecars are
  mostly NESTED archives: the first 3 sidecars on K are zips found INSIDE
  the WHMCS tar.gz files, each with its own fingerprint.

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

- **Phase 1 — DONE (2026-11-08, `6cd6691`)**: core/archives.py
  (extract + fingerprint + ingest + manifest build/write), DB tables
  (base schema + migration 3, apply_migrations runs multi-statement now),
  `backup` wiring, `backupme archive list|show|restore`, MANUAL 7.7 +
  §10 rows, tests/test_archives.py (17 scenarios). Notable engine decisions:
  gzip/bz2/xz classified only when a tar stream is inside (plain .gz files
  keep their old behavior); truncated zip still takes the explicit fallback;
  nested members get a virtual chunkless files row; unsafe members skipped
  and counted (cpanel style); nested failure stays scoped (kept as a whole
  member of its parent, not a global fallback).
- **DONE (2026-11-08, `912bb2f`)**: off-site guard — core/offsite_guard.py +
  daily scheduler tick + `db snapshot --offsite` + `guard` config + MANUAL
  7.8 + tests/test_offsite_guard.py (16). Remote path
  `guard/snapshots/<UTC ts>-gen<gen>.db.zst`, size-verified per backend.
- **Phase 2 — IN FLIGHT (two agents, 2026-11-08)**:
  - Agent G (formats): 7z via py7zr (lazy import; missing package → stays
    unsupported with a named reason), rar via external unrar/7z tool when
    present (PATH + WinRAR/7-Zip install paths, cached once per process;
    subprocess without shell=True, staging walk, timeout 600s), encrypted/
    corrupt → ArchiveFallback → stored whole. Files: core/archives.py,
    dependency file, tests/test_archive_formats.py.
  - Agent H (surface): dashboard Archives page (GET /archives +
    /api/archives[/id] + confirm-gated POST /api/archives/restore via
    to_thread), `backupme db recover-from-manifests BACKEND --into PATH
    [--verify full|none] [--dry-run]` (rebuilds a complete vault map into a
    NEW sqlite file from manifest sidecars; never opens the live DB; full
    verify = decompress-hash per blob per the D8 lesson; unverifiable
    members are reported as problems and get NO file rows). Files:
    web/routes/archives.py, template, routes/__init__.py, nav,
    core/manifest_recover.py, cli/db_cmd.py, MANUAL.
- **Phase 3 — IN FLIGHT (2026-11-08, main thread)**: re-ingesting the 20
  Softaculous .tar.gz archives from their source files through
  `ingest_archive_file` (targets = K only), three gated phases: A ingest
  (abort unless every archive returns archive_ingested), B sha-verify
  (ALL members of the 2 smallest archives + first/last member of every
  other archive), C soft-delete the 20 old whole-blob rows via
  `_soft_delete_file()` + op_log audit (core op_log API: append /
  mark_committed / mark_failed — the web-layer op_log_start/finish wrappers
  do NOT exist in core). Zero-ref blobs remain physically on K until the
  user approves `gc --apply`.
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
