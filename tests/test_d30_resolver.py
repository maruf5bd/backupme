"""Defect D30: the ONE backend resolver — one cache, one serializer, one
template resolution point, honest unreachability.

Proves:
- the web layer and the CLI resolve the SAME config to the SAME instance
  objects through one shared cache (one SSH session, not two);
- the cache key includes the backend's LOCATION identity (the D37 lesson:
  same name + different root never shares an instance, and a root move
  builds a fresh instance);
- an unreachable backend is resolved honestly (connected=False, listed by
  unreachable_backends, logged loudly by get_all_backends) and sits out a
  short connect cooldown instead of being re-dialed per request; an
  explicit connect() is never gated by the cooldown;
- the web serializer emits config_path/extra_args/timeout_s (rclone keeps
  --config on every web path) and matches the CLI entry dict exactly;
- {{ ENV }} templates resolve in config.load_config — the ONE resolution
  point for both layers; a missing variable resolves to "" (documented
  behavior of config._resolve_env, pinned here);
- the web status probe respects the cooldown instead of re-dialing.
"""
import sys
import time
import types
from pathlib import Path

import pytest

from backupme.config import load_config
from backupme.backends import (available_backends, get_all_backends,
                               get_backend, invalidate_cache,
                               resolve_all_backends, resolve_backend,
                               unreachable_backends)
from backupme.web.routes.common import RouteContext, _config_dict, _reload_config


# ---------------------------------------------------------------------------
# Config helpers (scratch config.yaml inside the env fixture root)
# ---------------------------------------------------------------------------

def _write_yaml(env, body: str) -> None:
    (Path(env) / "config.yaml").write_text(body, encoding="utf-8")


def _sftp_yaml(name: str = "box", password: str = "p") -> str:
    return (
        "backends:\n"
        "  sftp:\n"
        f"  - name: {name}\n"
        "    host: storage.example\n"
        "    port: 23\n"
        "    username: u\n"
        f"    password: \"{password}\"\n"
        "    path: /home/u\n"
        "encryption:\n"
        "  enabled: false\n"
    )


def _local_yaml(entries: list[tuple[str, Path]]) -> str:
    lines = ["backends:", "  local:"]
    for name, path in entries:
        lines.append(f"  - name: {name}")
        lines.append(f"    path: {path.as_posix()}")
        lines.append("    priority: 100")
    lines.append("encryption:")
    lines.append("  enabled: false")
    return "\n".join(lines) + "\n"


def _refused_socket(monkeypatch) -> dict:
    """Stub the ftp_sftp socket module so every dial is refused. The count
    doubles as the connect-attempt counter (refused fails fast, 1 dial)."""
    import socket as real_socket
    import backupme.backends.ftp_sftp as ftp_sftp

    calls = {"n": 0}

    def refused_connect(addr, timeout=None):
        calls["n"] += 1
        raise ConnectionRefusedError(10061, "actively refused")

    monkeypatch.setattr(
        ftp_sftp, "socket",
        types.SimpleNamespace(create_connection=refused_connect,
                              timeout=real_socket.timeout,
                              error=real_socket.error,
                              gaierror=real_socket.gaierror))
    return calls


# ---------------------------------------------------------------------------
# Fake paramiko (adapted from tests/test_sftp_connect.py — no real network)
# ---------------------------------------------------------------------------

class FakeSFTPClient:
    def __init__(self, transport):
        self.transport = transport

    @classmethod
    def from_transport(cls, transport):
        return cls(transport)

    def chdir(self, path):
        self.last_chdir = path


class FakeTransport:
    instances: list = []

    def __init__(self, sock):
        self.sock = sock
        self.connect_calls = 0
        self.closed = False
        FakeTransport.instances.append(self)

    def connect(self, username=None, password=None, pkey=None):
        self.connect_calls += 1

    def set_keepalive(self, interval: int):
        self.keepalive_interval = interval

    def close(self):
        self.closed = True


class FakeSock:
    def close(self):
        pass


@pytest.fixture()
def fake_paramiko(monkeypatch):
    mod = types.ModuleType("paramiko")

    class FakeSSHException(Exception):
        pass

    class FakeAuthenticationException(FakeSSHException):
        pass

    class FakeBadHostKeyException(FakeSSHException):
        pass

    mod.SSHException = FakeSSHException
    mod.AuthenticationException = FakeAuthenticationException
    mod.BadHostKeyException = FakeBadHostKeyException
    mod.Transport = FakeTransport
    mod.SFTPClient = FakeSFTPClient
    monkeypatch.setitem(sys.modules, "paramiko", mod)
    FakeTransport.instances.clear()
    yield mod
    FakeTransport.instances.clear()


@pytest.fixture()
def fake_socket(monkeypatch):
    import socket as real_socket
    import backupme.backends.ftp_sftp as ftp_sftp

    sock = FakeSock()
    monkeypatch.setattr(
        ftp_sftp, "socket",
        types.SimpleNamespace(create_connection=lambda addr, timeout=None: sock,
                              timeout=real_socket.timeout,
                              error=real_socket.error,
                              gaierror=real_socket.gaierror))
    return sock


def _total_connect_calls() -> int:
    return sum(t.connect_calls for t in FakeTransport.instances)


# ---------------------------------------------------------------------------
# 1. One resolver: web and CLI share one instance (one SSH session)
# ---------------------------------------------------------------------------

def test_web_and_cli_resolve_same_instance_one_ssh_session(
        env, fake_paramiko, fake_socket):
    """Same config -> the web layer and the CLI layer get the SAME instance
    object from the ONE cache: one transport, one connect, not two."""
    _write_yaml(env, _sftp_yaml())
    invalidate_cache()

    ctx = RouteContext(None, load_config(), None)
    web_list = _web_backends(ctx)
    cli_list = get_all_backends(load_config())

    assert web_list and cli_list
    assert web_list[0] is cli_list[0], \
        "web and CLI must share the one instance cache (defect D30)"
    assert get_backend("box", load_config()) is web_list[0]
    assert _total_connect_calls() == 1, "one SSH session, not two"
    assert len(FakeTransport.instances) == 1


def test_same_config_resolves_one_instance_repeatedly(env):
    """The cache returns the same object for repeated resolves of the same
    config (the old web pool vs core cache split resolved twice)."""
    _write_yaml(env, _local_yaml([("dup", Path(env).parent / "loc_a")]))
    (Path(env).parent / "loc_a").mkdir(parents=True, exist_ok=True)
    invalidate_cache()

    a = resolve_backend("dup", load_config())
    b = resolve_backend("dup", load_config())
    again = _web_backends(RouteContext(None, load_config(), None))
    assert a is b
    assert again[0] is a


def _web_backends(ctx):
    from backupme.web.routes.common import _get_backends
    return _get_backends(ctx)


# ---------------------------------------------------------------------------
# 2. The cache key includes the LOCATION identity (D37 lesson)
# ---------------------------------------------------------------------------

def test_same_name_different_roots_resolve_distinct_instances(env, tmp_path):
    """Two config entries with the SAME name on different roots must never
    share an instance — the cache key carries the location fingerprint."""
    root_a = Path(env).parent / "twin_a"
    root_b = Path(env).parent / "twin_b"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)
    _write_yaml(env, _local_yaml([("twin", root_a), ("twin", root_b)]))
    invalidate_cache()

    backs = resolve_all_backends(load_config())
    assert len(backs) == 2, backs
    assert backs[0] is not backs[1]
    paths = {b.base_path for b in backs}
    assert len(paths) == 2, \
        f"same name on two roots collapsed into one instance: {paths}"


def test_root_move_builds_a_fresh_instance(env):
    """The D37 scenario: same backend name, moved root. The moved backend
    must get a fresh instance — never the old root's instance or bytes."""
    root_a = Path(env).parent / "mover_a"
    root_b = Path(env).parent / "mover_b"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)

    _write_yaml(env, _local_yaml([("mover", root_a)]))
    invalidate_cache()
    old = resolve_backend("mover", load_config())

    _write_yaml(env, _local_yaml([("mover", root_b)]))
    new = resolve_backend("mover", load_config())

    assert new is not old
    assert Path(new.base_path).resolve() == root_b.resolve()
    assert Path(old.base_path).resolve() == root_a.resolve()


# ---------------------------------------------------------------------------
# 3. Honest unreachability: reported, cooled down, never silently dropped
# ---------------------------------------------------------------------------

def test_unreachable_backend_is_reported_and_cools_down(env, monkeypatch):
    """An unreachable backend is resolved honestly (connected=False, in
    unreachable_backends) instead of vanishing; within the cooldown the
    resolver does NOT re-dial on every request; after it expires the
    retry happens on the SAME instance (one object per key)."""
    _write_yaml(env, _sftp_yaml(name="dead"))
    refused = _refused_socket(monkeypatch)
    from backupme import backends as bmod
    invalidate_cache()

    cfg = load_config()
    all_backends = resolve_all_backends(cfg)
    assert len(all_backends) == 1
    dead = all_backends[0]
    assert dead.connected is False
    assert dead.last_connect_error, "the failed resolve carries its reason"
    assert refused["n"] == 1

    # Honest split: nothing silently dropped, gaps loud.
    assert available_backends(cfg) == []
    assert [b.name for b in unreachable_backends(cfg)] == ["dead"]

    # Pipeline contracts unchanged (connected-only), see test_backends_connect.
    assert get_backend("dead", cfg) is None
    assert get_all_backends(cfg) == []

    # Cooldown: repeated resolves inside the window do not re-dial.
    resolve_all_backends(load_config())
    resolve_all_backends(load_config())
    assert refused["n"] == 1, \
        "the failed-connect cooldown must prevent per-request reconnect storms"

    # Cooldown expired: the SAME failed instance is retried (no second
    # parallel session object).
    for key, (_deadline, fp, inst) in list(bmod._failed_cache.items()):
        bmod._failed_cache[key] = (0.0, fp, inst)
    again = resolve_all_backends(load_config())
    assert refused["n"] == 2
    assert again[0] is dead


def test_config_edit_during_cooldown_rebuilds_not_retries(env, monkeypatch):
    """A config edit while a backend is cooling down must NOT retry the old
    instance's stale credentials — the drifted failed entry is dropped and
    a fresh instance is built from the new config."""
    _write_yaml(env, _sftp_yaml(name="dead"))
    refused = _refused_socket(monkeypatch)
    from backupme import backends as bmod
    invalidate_cache()

    dead = resolve_backend("dead", load_config())
    assert dead.connected is False and refused["n"] == 1

    # Edit the config (new password) while the cooldown is live.
    _write_yaml(env, _sftp_yaml(name="dead", password="new-password"))
    fresh = resolve_backend("dead", load_config())
    assert fresh is not dead, \
        "a drifted failed entry must rebuild, not retry stale credentials"
    assert fresh.config["password"] == "new-password"


def test_get_all_backends_logs_the_gap(env, monkeypatch, caplog):
    """The pipeline-resolution helper stays connected-only, but a dropped
    backend is logged loudly instead of silently missing (guard/gc see the
    gap in the log; the honest programmatic list is unreachable_backends)."""
    _write_yaml(env, _sftp_yaml(name="dead"))
    _refused_socket(monkeypatch)
    invalidate_cache()

    import logging
    with caplog.at_level(logging.WARNING, logger="backupme.backends"):
        result = get_all_backends(load_config())
    assert result == []
    gap_rows = [r for r in caplog.records
                if "dead" in r.getMessage() and "unreachable" in r.getMessage()]
    assert gap_rows, "the unreachable backend must be named in the log, not dropped silently"


def test_explicit_connect_is_never_gated_by_cooldown(env, monkeypatch):
    """User-requested retries (backend test, rescan, speed test) call
    connect() on the resolved instance directly — the cooldown only gates
    the resolver's automatic re-dial."""
    _write_yaml(env, _sftp_yaml(name="dead"))
    refused = _refused_socket(monkeypatch)
    invalidate_cache()

    dead = resolve_backend("dead", load_config())
    assert dead.connected is False
    assert refused["n"] == 1
    assert dead.connect() is False  # explicit retry dials again immediately
    assert refused["n"] == 2


def test_status_probe_respects_cooldown(env, monkeypatch):
    """The web status row for a cooling-down backend reports the cached
    failure instead of re-dialing the remote on every status poll."""
    from backupme.web.routes.backends import _status_probe_row
    _write_yaml(env, _sftp_yaml(name="dead"))
    refused = _refused_socket(monkeypatch)
    invalidate_cache()

    dead = resolve_backend("dead", load_config())
    assert refused["n"] == 1
    assert time.monotonic() < dead.connect_retry_after, "cooldown armed"

    row = _status_probe_row(dead, "dead")
    assert row["connected"] is False
    assert row["error"], "an offline row must carry its failure reason"
    assert row["priority"] == dead.config.get("priority", 50)
    assert refused["n"] == 1, "the status probe must not re-dial during cooldown"


# ---------------------------------------------------------------------------
# 4. One serializer: web emits the same fields the CLI sees (rclone)
# ---------------------------------------------------------------------------

_RCLONE_YAML = (
    "backends:\n"
    "  rclone:\n"
    "  - name: gdrive\n"
    "    remote: gdrive:/backupme\n"
    "    config_path: /etc/rclone/rclone.conf\n"
    "    extra_args: [--sftp-set-modtime=false, --tpslimit=4]\n"
    "    timeout_s: 7200\n"
    "encryption:\n"
    "  enabled: false\n"
)


def test_web_and_cli_entries_match_with_rclone_connection_fields(env):
    """The ONE serializer: the web _config_dict entry carries
    config_path/extra_args/timeout_s and is byte-identical to the CLI's
    dataclass-derived entry (the old web field list dropped them, so
    rclone lost --config on every web path)."""
    _write_yaml(env, _RCLONE_YAML)
    invalidate_cache()

    from backupme.backends import _config_to_dict
    ctx = RouteContext(None, load_config(), None)
    web_entry = _config_dict(ctx)["rclone"][0]
    cli_entry = _config_to_dict(load_config().backends)["rclone"][0]

    assert web_entry["config_path"] == "/etc/rclone/rclone.conf"
    assert web_entry["extra_args"] == ["--sftp-set-modtime=false", "--tpslimit=4"]
    assert web_entry["timeout_s"] == 7200
    assert web_entry == cli_entry


def test_rclone_instance_keeps_config_path_across_layers(env, monkeypatch):
    """The resolved RcloneBackend actually carries --config fields, and the
    web layer resolves the SAME instance the CLI built."""
    _write_yaml(env, _RCLONE_YAML)
    invalidate_cache()

    from backupme.backends import RcloneBackend

    def fake_connect(self):
        self._connected = True
        return True

    monkeypatch.setattr(RcloneBackend, "connect", fake_connect)

    be = resolve_backend("gdrive", load_config())
    assert be.config_path == "/etc/rclone/rclone.conf"
    assert be.extra_args == ["--sftp-set-modtime=false", "--tpslimit=4"]
    assert be.timeout_s == 7200

    web = [b for b in _web_backends(RouteContext(None, load_config(), None))
           if b.name == "gdrive"]
    assert web == [be], "web must resolve the same instance the CLI built"


def test_config_fingerprint_rebuilds_on_credential_change(env):
    """A config edit within the SAME location produces a new drift
    fingerprint, so a cached instance never serves stale credentials."""
    from backupme.backends import config_fingerprint
    entry_before = {"name": "box", "password": "old", "host": "h",
                    "port": 23, "path": "/home/u"}
    entry_after = dict(entry_before, password="new")
    assert config_fingerprint(entry_before) != config_fingerprint(entry_after)


# ---------------------------------------------------------------------------
# 5. One template resolution point: config.load_config
# ---------------------------------------------------------------------------

def test_load_config_resolves_env_templates(env, monkeypatch):
    """{{ ENV }} templates resolve inside load_config — the ONE resolution
    point both layers share (defect D30)."""
    monkeypatch.setenv("D30_SFTP_PASSWORD", "s3cret-value")
    _write_yaml(env, _sftp_yaml(password="{{ D30_SFTP_PASSWORD }}"))
    cfg = load_config()
    assert cfg.backends["sftp"][0].password == "s3cret-value"


def test_load_config_missing_env_var_resolves_to_empty(env, monkeypatch):
    """Documented behavior of config._resolve_env: a missing variable
    resolves to an empty string (pinned so the contract stays explicit)."""
    monkeypatch.delenv("D30_MISSING_PASSWORD", raising=False)
    _write_yaml(env, _sftp_yaml(password="{{ D30_MISSING_PASSWORD }}"))
    cfg = load_config()
    assert cfg.backends["sftp"][0].password == ""


def test_web_reload_config_resolves_env_templates(env, monkeypatch):
    """web _reload_config delegates to load_config, so a web config reload
    can no longer authenticate with literal '{{ PASSWORD }}' strings (the
    defect: the old inline re-parse skipped template resolution)."""
    monkeypatch.setenv("D30_SFTP_PASSWORD", "s3cret-value")
    _write_yaml(env, _sftp_yaml(password="{{ D30_SFTP_PASSWORD }}"))

    import backupme.config as cfgmod
    ctx = RouteContext(None, cfgmod.Config(), None)  # stale/empty app config
    _reload_config(ctx)
    assert ctx.config.backends["sftp"][0].password == "s3cret-value"

    # ...and the resolver entry dicts carry the resolved value too.
    entry = _config_dict(ctx)["sftp"][0]
    assert entry["password"] == "s3cret-value"


def test_web_reload_config_tolerates_unknown_backup_keys(env):
    """The old web reload crashed with TypeError on `backups:` keys the CLI
    tolerates (_filter_fields); load_config warns and keeps the known ones."""
    _write_yaml(
        env,
        "backups:\n"
        "- source: docs\n"
        "  unknown_future_key: 1\n"
        "encryption:\n"
        "  enabled: false\n",
    )

    import backupme.config as cfgmod
    ctx = RouteContext(None, cfgmod.Config(), None)
    _reload_config(ctx)  # must not raise
    assert [b.source for b in ctx.config.backups] == ["docs"]
