import dataclasses

import pytest

from backupme.backends.local import LocalBackend
from backupme.config import load_config
from backupme.core.database import execute
from backupme.core.uploader import upload_bundle, _do_upload
from tests.helpers import load_cfg


def _cfg():
    return dataclasses.asdict(load_cfg())


def _chunk_rows():
    return execute(
        "SELECT compression_algo FROM blob_storage WHERE storage_path LIKE 'objects/%'"
    ).fetchall()


def test_min_size_threshold(env, backends):
    big = env / "big.bin"
    big.write_bytes(bytes(range(256)) * (3 * 1024 * 1024 // 256))
    small = env / "small.bin"
    small.write_bytes(b"x" * 1024)
    d = _cfg()
    d["chunking"]["min_size"] = 2 * 1024 * 1024
    d["chunking"]["chunk_size"] = 8 * 1024 * 1024
    results = upload_bundle(
        [
            {"local_path": str(big), "source_root": str(env), "relative_path": "big.bin"},
            {"local_path": str(small), "source_root": str(env), "relative_path": "small.bin"},
        ],
        backends, d, 1,
    )
    big_chunked = execute("SELECT chunked FROM files WHERE id = ?", (results[0]["file_id"],)).fetchone()[0]
    small_chunked = execute("SELECT chunked FROM files WHERE id = ?", (results[1]["file_id"],)).fetchone()[0]
    assert big_chunked == 1 and small_chunked == 0


def test_compression_disabled(env, backends):
    (env / "c.csv").write_bytes(bytes(range(256)) * (12 * 1024 * 1024 // 256))
    d = _cfg()
    d["compression"]["enabled"] = False
    upload_bundle(
        [{"local_path": str(env / "c.csv"), "source_root": str(env), "relative_path": "c.csv"}],
        backends, d, 1,
    )
    rows = _chunk_rows()
    assert rows and all(r["compression_algo"] is None for r in rows)


def test_compression_override_skip(env, backends):
    # Overrides apply to individually-compressed (chunked) files: 12 MB > min_size.
    (env / "x.bin").write_bytes(bytes(range(256)) * (12 * 1024 * 1024 // 256))
    d = _cfg()
    d["compression"]["overrides"] = [{"ext": ".bin", "skip": True}]
    upload_bundle(
        [{"local_path": str(env / "x.bin"), "source_root": str(env), "relative_path": "x.bin"}],
        backends, d, 1,
    )
    rows = _chunk_rows()
    assert rows and all(r["compression_algo"] is None for r in rows)


def test_compression_override_gzip(env, backends):
    (env / "g.csv").write_bytes(bytes(range(256)) * (12 * 1024 * 1024 // 256))
    d = _cfg()
    d["compression"]["overrides"] = [{"ext": ".csv", "algorithm": "gzip", "level": 6}]
    upload_bundle(
        [{"local_path": str(env / "g.csv"), "source_root": str(env), "relative_path": "g.csv"}],
        backends, d, 1,
    )
    rows = _chunk_rows()
    assert rows and all(r["compression_algo"] == "gzip" for r in rows)


def test_verification_retries_honored(tmp_path, monkeypatch):
    calls = {"n": 0}

    class DownBackend(LocalBackend):
        def upload(self, lp, rp):
            calls["n"] += 1
            raise RuntimeError("down")

        def hash(self, p):
            raise RuntimeError("no hash")

        def download(self, rp, lp):
            raise RuntimeError("nothing there")

    bk = DownBackend("down", {"path": str(tmp_path / "store")})
    bk.connect()
    src = tmp_path / "data.bin"
    src.write_bytes(b"x" * 100)
    import hashlib
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    assert _do_upload(bk, str(src), "objects/aa/bb/x", sha, retries=2) is False
    assert calls["n"] == 2


def test_scheduler_honors_backend_key(env, backends, tmp_path):
    from tests.helpers import add_backend
    import backupme.scheduler as sched_mod
    from backupme.scheduler import BackupScheduler

    add_backend(env, "second", tmp_path / "storage2")
    (env / "pick.txt").write_bytes(b"pick me " * 50)
    cfg = _cfg()
    cfg["backups"] = [{"source": str(env), "backend": "second"}]
    BackupScheduler(cfg)._run_backups()
    used = execute("SELECT DISTINCT backend_name FROM blob_storage").fetchall()
    assert [r["backend_name"] for r in used] == ["second"]


def test_move_field_honored_by_scheduler(env, backends):
    from backupme.scheduler import BackupScheduler
    data_dir = env / "data"
    data_dir.mkdir()
    (data_dir / "gone.txt").write_bytes(b"move me " * 50)
    cfg = _cfg()
    cfg["backups"] = [{"source": str(data_dir), "move": True}]
    BackupScheduler(cfg)._run_backups()
    assert not (data_dir / "gone.txt").exists()


def test_unknown_config_keys_tolerated(env, caplog):
    cfg_path = env / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() +
                        "chunking:\n  chunk_size: 64MB\n  parallel_uploads: 4\n")
    import logging
    with caplog.at_level(logging.WARNING):
        cfg = load_config()
    assert cfg.chunking.chunk_size == 67108864
    assert any("parallel_uploads" in r.message for r in caplog.records)
