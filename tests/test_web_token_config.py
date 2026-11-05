"""Regression tests for qa defect D1: a web.token set in config.yaml must
reach WebConfig and arm the /api/* bearer gate.

load_config() used to have no `web` branch, so the token stayed "" and the
whole admin API served unauthenticated even with web.token configured.
The CLI path (`backupme web --token X`) overrides cfg.web.token only when the
flag is passed, so arming via config must preserve that precedence.
"""
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from backupme.config import load_config
from backupme.web.app import create_app

TOKEN = "qa-secret-token"


def _write_web_section(env, body: str):
    cfg_path = Path(env) / "config.yaml"
    cfg_path.write_text(cfg_path.read_text() + "web:\n" + body)


def test_load_config_parses_web_section(env):
    _write_web_section(env, f"  token: {TOKEN}\n  port: 8443\n  host: 0.0.0.0\n")
    cfg = load_config()
    assert cfg.web.token == TOKEN
    assert cfg.web.port == 8443
    assert cfg.web.host == "0.0.0.0"
    # Untouched defaults survive the section parse.
    assert cfg.web.enabled is True


def test_load_config_without_web_section_keeps_open_default(env):
    cfg = load_config()
    assert cfg.web.token == ""
    assert cfg.web.port == 8080


def test_load_config_web_unknown_keys_warn_but_token_survives(env, caplog):
    _write_web_section(env, f"  token: {TOKEN}\n  bogus_option: 1\n")
    with caplog.at_level(logging.WARNING):
        cfg = load_config()
    assert cfg.web.token == TOKEN
    assert any("bogus_option" in r.message for r in caplog.records)


def _client(env):
    app = create_app(load_config())
    return TestClient(app)


def test_config_token_rejects_missing_and_wrong_bearer(env):
    _write_web_section(env, f"  token: {TOKEN}\n")
    client = _client(env)
    assert client.get("/api/status").status_code == 401
    wrong = client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}-wrong"})
    assert wrong.status_code == 401
    # Exact 'Bearer ' scheme match is preserved (qa auth matrix).
    lower = client.get("/api/status", headers={"Authorization": f"bearer {TOKEN}"})
    assert lower.status_code == 401
    ok = client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200


def test_config_token_keeps_pages_public(env):
    """Only /api/* sits behind the gate; dashboard pages stay public by design
    (loopback binding is the documented mitigation)."""
    _write_web_section(env, f"  token: {TOKEN}\n")
    client = _client(env)
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200


def test_cli_token_override_wins_over_config_token(env):
    """web_cmd.py arms cfg.web.token = <--token> when the flag is passed;
    that override must replace (not lose to) the config.yaml token."""
    _write_web_section(env, f"  token: {TOKEN}\n")
    cfg = load_config()
    assert cfg.web.token == TOKEN
    # Mirror of web_cmd: `backupme web --token cli-token`
    cfg.web.token = "cli-token"
    app = create_app(cfg)
    client = TestClient(app)
    assert client.get("/api/status", headers={"Authorization": "Bearer cli-token"}).status_code == 200
    stale = client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert stale.status_code == 401
