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
