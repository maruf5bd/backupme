"""Tests for `backupme init` (audit_pro_product.md P0-2) and the two related
front-door fixes:

- init scaffolds config.yaml from config.example.yaml + creates the DB,
  and refuses (exit 0, no changes) when a config already exists;
- a missing/empty config.yaml now logs a loud warning instead of silently
  falling back to defaults (P0-2 quick win);
- `--help` invocations create NO backupme.db in the CWD (P1-1) while real
  commands still do.
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.cli import cli
from backupme.config import load_config

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = REPO_ROOT / "config.example.yaml"


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """Empty working directory standing in for a fresh user's folder."""
    d = tmp_path / "fresh_dir"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


def _run_cli_subprocess(cwd, *args):
    """Run the real CLI entry in a subprocess pinned to `cwd`.

    Uses sys.argv exactly like a user's shell would (the `python -c` driver
    places the CLI args in argv[1:]), which is what base.py's help detection
    inspects. BACKUPME_CONFIG is removed so the run sees a fresh directory.
    """
    env = dict(os.environ)
    env.pop("BACKUPME_CONFIG", None)
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", "from backupme.cli import cli; cli()", *args],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120,
    )


# ---------------------------------------------------------------------------
# init: fresh directory
# ---------------------------------------------------------------------------


def test_init_fresh_creates_config_and_db(runner, workdir):
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    config = workdir / "config.yaml"
    db = workdir / "backupme.db"
    assert config.is_file(), "init must scaffold config.yaml"
    assert db.is_file(), "init must create the SQLite database"
    # Byte-for-byte scaffold from the repo's test-pinned example template.
    assert config.read_bytes() == EXAMPLE_PATH.read_bytes()
    # Human output is actionable: what happened + concrete next steps + docs.
    assert "Created config.yaml" in result.output
    assert "Created database" in result.output
    assert "Next steps" in result.output
    assert "backend add" in result.output
    assert "backup <path>" in result.output
    assert "docs/MANUAL.md" in result.output


def test_init_config_example_yaml_untouched(runner, workdir):
    before = hashlib.sha256(EXAMPLE_PATH.read_bytes()).hexdigest()
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    after = hashlib.sha256(EXAMPLE_PATH.read_bytes()).hexdigest()
    assert before == after, "init must copy FROM config.example.yaml, never touch it"


def test_init_respects_custom_config_path(runner, workdir):
    result = runner.invoke(cli, ["--config", "custom.yaml", "init"])
    assert result.exit_code == 0, result.output
    assert (workdir / "custom.yaml").is_file()
    assert not (workdir / "config.yaml").exists()


def test_init_without_example_template_still_initializes_db(runner, workdir, monkeypatch):
    """Non-editable installs may not carry config.example.yaml: the command
    must say so loudly, still create the DB, and stay exit 0."""
    from backupme.cli import init_cmd
    monkeypatch.setattr(init_cmd, "_find_example_file", lambda: None)
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    assert not (workdir / "config.yaml").exists()
    assert (workdir / "backupme.db").is_file()
    assert "WARNING" in result.output
    assert "config.example.yaml" in result.output


# ---------------------------------------------------------------------------
# init: already-initialized directory (idempotent refusal, exit 0)
# ---------------------------------------------------------------------------


def test_init_second_run_refuses_without_changes(runner, workdir):
    first = runner.invoke(cli, ["init"])
    assert first.exit_code == 0, first.output
    config = workdir / "config.yaml"
    before = config.read_bytes()
    # Mutate the scaffold so an overwrite would be detectable.
    config.write_bytes(before + b"# user edit\n")
    second = runner.invoke(cli, ["init"])
    assert second.exit_code == 0, second.output
    assert config.read_bytes() == before + b"# user edit\n"
    assert "already exists" in second.output
    assert "never overwrites" in second.output


def test_init_json_payload_fresh(runner, workdir):
    result = runner.invoke(cli, ["init", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)  # strict: stdout must parse on its own
    assert data["status"] == "initialized"
    assert data["config_created"] is True
    assert data["database_initialized"] is True
    assert data["config"] == "config.yaml"
    assert data["database"] == "backupme.db"
    assert isinstance(data["next_steps"], list) and len(data["next_steps"]) == 3


def test_init_json_payload_already_initialized(runner, workdir):
    first = runner.invoke(cli, ["init", "--json"])
    assert first.exit_code == 0, first.output
    json.loads(first.stdout)
    second = runner.invoke(cli, ["init", "--json"])
    assert second.exit_code == 0, second.output
    data = json.loads(second.stdout)
    assert data["status"] == "already_initialized"
    assert data["config_created"] is False
    assert data["database_initialized"] is False


# ---------------------------------------------------------------------------
# P0-2 quick win: missing / empty config now warns instead of staying silent
# ---------------------------------------------------------------------------


def test_missing_config_logs_warning(tmp_path, caplog):
    missing = tmp_path / "nowhere" / "config.yaml"
    with caplog.at_level(logging.WARNING):
        cfg = load_config(str(missing))
    assert cfg is not None  # defaults still work — loudly
    warnings = [r for r in caplog.records if "nowhere" in r.getMessage()]
    assert warnings, "a missing config must produce a named-path warning"
    msg = warnings[0].getMessage()
    assert str(missing) in msg
    assert "defaults" in msg
    assert "backupme init" in msg


def test_empty_config_logs_warning(tmp_path, caplog):
    empty = tmp_path / "config.yaml"
    empty.write_text("", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        cfg = load_config(str(empty))
    assert cfg is not None
    warnings = [r for r in caplog.records if str(empty) in r.getMessage()]
    assert warnings, "an existing-but-empty config must warn, not silently default"
    assert "empty" in warnings[0].getMessage()
    assert "defaults" in warnings[0].getMessage()


def test_present_config_does_not_log_default_warning(tmp_path, caplog):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("encryption:\n  enabled: false\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        load_config(str(cfg_path))
    assert not [r for r in caplog.records if "defaults are in effect" in r.getMessage()]


# ---------------------------------------------------------------------------
# P1-1 regression: --help must not create backupme.db (real subprocess argv)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, expected_rc",
    [
        (("--help",), 0),
        (("db", "--help"), 0),
        # -h is NOT a valid option in this CLI (click's default help option is
        # --help only): click exits 2 with a usage error — and since the group
        # callback ran first and saw "-h", it must also have skipped DB init.
        (("backup", "-h"), 2),
    ],
)
def test_help_invocations_create_no_db(workdir, args, expected_rc):
    result = _run_cli_subprocess(workdir, *args)
    assert result.returncode == expected_rc, result.stderr or result.stdout
    assert not (workdir / "backupme.db").exists(), (
        f"backupme {' '.join(args)} must not create a database"
    )


def test_real_command_still_creates_db(workdir):
    """Positive control for the help-skip: a real command (no --help) still
    initializes the database exactly as before."""
    result = _run_cli_subprocess(workdir, "stats")
    assert result.returncode == 0, result.stderr or result.stdout
    assert (workdir / "backupme.db").is_file()


def test_option_after_separator_does_not_skip_init(workdir):
    """`backupme backup -- -h` passes a real argument named '-h'; click runs
    the command body, so the group callback must not treat it as help."""
    result = _run_cli_subprocess(workdir, "stats", "--", "-h")
    # stats takes no arguments; click exits with usage code 2 — but the DB
    # must have been created by the (non-skipped) group callback either way.
    assert not result.returncode == 0  # usage error, NOT a help exit (0)
    assert (workdir / "backupme.db").is_file()
