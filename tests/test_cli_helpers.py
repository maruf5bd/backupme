"""Tests for shared CLI helpers: backupme.cli.output and backupme.cli.progress."""
import json
from pathlib import Path

import pytest

from backupme.cli import output as output_mod
from backupme.cli import progress as progress_mod


# --- emit: json mode ---


def test_emit_json_dict_roundtrip_with_stable_keys(capsys):
    data = {"count": 3, "name": "alpha", "nested": {"lossy": [1, 2]}}
    output_mod.emit(data, as_json=True)
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == data
    assert list(parsed) == ["count", "name", "nested"]  # key order stable


def test_emit_json_uses_default_str_for_unserializable(capsys):
    output_mod.emit({"path": Path("/tmp/x")}, as_json=True)
    out = capsys.readouterr().out
    assert "x" in json.loads(out)["path"]


# --- emit: human mode ---


def test_emit_human_calls_custom_renderer(capsys):
    seen = []
    output_mod.emit({"k": "v"}, as_json=False, human=seen.append)
    assert seen == [{"k": "v"}]
    out = capsys.readouterr().out
    assert "k: v" not in out  # default table renderer not invoked


def test_emit_default_renders_dict_as_key_value_lines(capsys):
    output_mod.emit({"files": 10, "blobs": 4}, as_json=False)
    out = capsys.readouterr().out
    assert out == "files: 10\nblobs: 4\n"


def test_emit_default_renders_empty_list_as_none(capsys):
    output_mod.emit([], as_json=False)
    out = capsys.readouterr().out
    assert out.strip() == "(none)"


def test_emit_default_renders_scalar_list_one_per_line(capsys):
    output_mod.emit(["alpha", "beta"], as_json=False)
    out = capsys.readouterr().out
    assert out == "alpha\nbeta\n"


def test_emit_default_renders_dict_list_as_aligned_columns(capsys):
    rows = [
        {"name": "a.txt", "size": 12},
        {"name": "b.txt", "sha": "x" * 50},
    ]
    output_mod.emit(rows, as_json=False)
    out = capsys.readouterr().out
    lines = out.splitlines()
    for key in ("name", "size", "sha"):  # union of keys forms the header
        assert key in lines[0]
    assert "a.txt" in out and "b.txt" in out
    assert "x" * 40 in out  # truncated to 40 chars
    assert "x" * 50 not in out


# --- Progress: fallback rendering (tqdm forced off) ---


def test_progress_fallback_with_total(capsys, monkeypatch):
    monkeypatch.setattr(progress_mod, "_HAS_TQDM", False)
    p = progress_mod.Progress(True, total=10, desc="work")
    p.update(3)
    assert "\rwork: 3/10 (30%)" in capsys.readouterr().out
    p.update(7)
    assert "\rwork: 10/10 (100%)" in capsys.readouterr().out
    p.finish()
    final = capsys.readouterr().out
    assert final.startswith("\n")
    assert "work: done (10)" in final


def test_progress_fallback_without_total_shows_question_mark(capsys, monkeypatch):
    monkeypatch.setattr(progress_mod, "_HAS_TQDM", False)
    p = progress_mod.Progress(True, desc="scan")
    p.update()
    out = capsys.readouterr().out
    assert "\rscan: 1/?" in out
    assert "(?%)" in out


def test_progress_fallback_set_total_mid_stream(capsys, monkeypatch):
    monkeypatch.setattr(progress_mod, "_HAS_TQDM", False)
    p = progress_mod.Progress(True, desc="ingest")
    p.update(2)
    assert "\ringest: 2/?" in capsys.readouterr().out
    p.set_total(5)
    p.update(3)
    assert "\ringest: 5/5 (100%)" in capsys.readouterr().out
    p.finish()
    assert "ingest: done (5)" in capsys.readouterr().out


def test_progress_disabled_is_noop(capsys, monkeypatch):
    monkeypatch.setattr(progress_mod, "_HAS_TQDM", False)
    p = progress_mod.Progress(False, total=5, desc="quiet")
    p.update(2)
    p.set_total(3)
    p.update(1)
    p.finish()
    assert capsys.readouterr().out == ""


# --- Progress: tqdm path ---


def test_progress_tqdm_path_smoke(monkeypatch):
    pytest.importorskip("tqdm")
    monkeypatch.setattr(progress_mod, "_HAS_TQDM", True)
    with progress_mod.Progress(True, total=2, desc="tq") as p:
        p.update()
        p.set_total(4)
        p.update(3)


def test_progress_module_detection_matches_environment(monkeypatch):
    import importlib.util
    has_tqdm = importlib.util.find_spec("tqdm") is not None
    assert progress_mod._HAS_TQDM == has_tqdm


def test_progress_fallback_by_natural_detection(capsys):
    if progress_mod._HAS_TQDM:
        pytest.skip("tqdm installed; natural fallback not exercisable")
    p = progress_mod.Progress(True, total=1, desc="nat")
    p.update()
    assert "\rnat: 1/1 (100%)" in capsys.readouterr().out
