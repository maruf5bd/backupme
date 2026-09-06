from backupme.core.database import execute
from backupme.core.uploader import upload_bundle
from tests.helpers import load_cfg


def test_chunk_size_config_respected(env, backends):
    p = env / "twelve.mb"
    p.write_bytes(bytes(range(256)) * (12 * 1024 * 1024 // 256))
    cfg = load_cfg()
    d = __import__("dataclasses").asdict(cfg)
    d["chunking"]["chunk_size"] = 4 * 1024 * 1024
    results = upload_bundle(
        [{"local_path": str(p), "source_root": str(env), "relative_path": "three.mb"}],
        backends, d, 1,
    )
    chunks = execute(
        "SELECT COUNT(*) FROM file_chunks WHERE file_id = ?", (results[0]["file_id"],)
    ).fetchone()[0]
    assert chunks == 3
