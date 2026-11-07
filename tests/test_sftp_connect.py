"""SFTP connect hardening tests (Storage Box rate-limit resilience, defect
D10 follow-up).

paramiko is stubbed with a fake module injected into sys.modules — no real
network is ever touched. Proves:
- concurrent connects single-flight on one per-instance lock and reuse the
  live connection,
- transient SSHException failures retry (3 attempts, 0.5s/1.5s backoff),
- AuthenticationException fails immediately with no retry and no sleep.
"""
import socket
import sys
import threading
import time
import types

import pytest

import backupme.backends.ftp_sftp as ftp_sftp
from backupme.backends.ftp_sftp import SFTPBackend


# ---------------------------------------------------------------------------
# Fake paramiko
# ---------------------------------------------------------------------------

class FakeSSHException(Exception):
    pass


class FakeAuthenticationException(FakeSSHException):
    pass  # mirrors paramiko: AuthenticationException subclasses SSHException


class FakeBadHostKeyException(FakeSSHException):
    pass


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

    def close(self):
        self.closed = True


@pytest.fixture()
def fake_paramiko(monkeypatch):
    """Inject a fake `paramiko` module; reset the transport registry."""
    mod = types.ModuleType("paramiko")
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
def no_sleep(monkeypatch):
    """Replace the ftp_sftp module's `time` global so backoff never slows
    the tests; every requested sleep is recorded instead."""
    sleeps: list[float] = []
    monkeypatch.setattr(ftp_sftp, "time",
                        types.SimpleNamespace(sleep=sleeps.append))
    return sleeps


class FakeSock:
    def close(self):
        pass


@pytest.fixture()
def fake_socket(monkeypatch):
    """Stub the ftp_sftp module's `socket` global: _open_transport opens the
    TCP socket before touching paramiko, so it must not dial a real host.
    socket.timeout stays real (connect's transient-error tuple reads it)."""
    sock = FakeSock()
    monkeypatch.setattr(
        ftp_sftp, "socket",
        types.SimpleNamespace(create_connection=lambda addr, timeout=None: sock,
                              timeout=socket.timeout, error=socket.error,
                              gaierror=socket.gaierror))
    return sock


def _backend() -> SFTPBackend:
    return SFTPBackend("box", {"host": "storage.example", "port": 23,
                               "username": "u", "password": "p",
                               "path": "/home/u"})


def _total_connect_calls() -> int:
    return sum(t.connect_calls for t in FakeTransport.instances)


# ---------------------------------------------------------------------------
# Single-flight + reuse
# ---------------------------------------------------------------------------

def test_connect_single_flights_concurrent_callers(fake_paramiko, no_sleep, fake_socket):
    release = threading.Event()
    entered = threading.Event()

    def slow_connect(self, username=None, password=None, pkey=None):
        self.connect_calls += 1
        entered.set()
        release.wait(timeout=5)

    orig_connect = FakeTransport.connect
    FakeTransport.connect = slow_connect
    try:
        backend = _backend()
        results: list[bool] = []

        def worker():
            results.append(backend.connect())

        t1 = threading.Thread(target=worker, daemon=True)
        t1.start()
        assert entered.wait(timeout=5), "first connect never started"

        # Second caller arrives while the first is mid-connect: it must wait
        # on the per-instance lock instead of opening a second SSH session.
        t2 = threading.Thread(target=worker, daemon=True)
        t2.start()
        time.sleep(0.2)
        assert _total_connect_calls() == 1, \
            "concurrent connect must single-flight, not stampede"

        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert results == [True, True], results
        # The second caller reused the now-live connection: still one transport.
        assert _total_connect_calls() == 1, \
            "second caller must reuse the existing connection"
    finally:
        FakeTransport.connect = orig_connect


def test_connect_reuses_existing_connection(fake_paramiko, no_sleep, fake_socket):
    backend = _backend()
    assert backend.connect() is True
    assert backend.connected is True
    assert _total_connect_calls() == 1
    assert backend.connect() is True
    assert _total_connect_calls() == 1, "connected instance must be reused"
    assert FakeTransport.instances[0].closed is False


# ---------------------------------------------------------------------------
# Transient retry with backoff
# ---------------------------------------------------------------------------

def test_connect_retries_transient_then_succeeds(fake_paramiko, no_sleep, fake_socket):
    state = {"fails_left": 2}

    def flaky_connect(self, username=None, password=None, pkey=None):
        self.connect_calls += 1
        if state["fails_left"] > 0:
            state["fails_left"] -= 1
            raise FakeSSHException("Unable to open channel")

    orig_connect = FakeTransport.connect
    FakeTransport.connect = flaky_connect
    try:
        backend = _backend()
        assert backend.connect() is True, "third attempt must succeed"
        assert backend.connected is True
        # 3 attempts total: two transient failures, then success.
        assert _total_connect_calls() == 3, _total_connect_calls()
        assert no_sleep == [0.5, 1.5], no_sleep
    finally:
        FakeTransport.connect = orig_connect


def test_connect_returns_false_after_exhausted_retries(fake_paramiko, no_sleep, fake_socket):
    def always_fail(self, username=None, password=None, pkey=None):
        self.connect_calls += 1
        raise FakeSSHException("rate limited")

    orig_connect = FakeTransport.connect
    FakeTransport.connect = always_fail
    try:
        backend = _backend()
        assert backend.connect() is False
        assert _total_connect_calls() == 3, "exactly 3 attempts, no more"
        assert backend.connected is False
        assert no_sleep == [0.5, 1.5], no_sleep
    finally:
        FakeTransport.connect = orig_connect


# ---------------------------------------------------------------------------
# Auth failures never retry
# ---------------------------------------------------------------------------

def test_connect_auth_failure_fails_immediately(fake_paramiko, no_sleep, fake_socket):
    def auth_fail(self, username=None, password=None, pkey=None):
        self.connect_calls += 1
        raise FakeAuthenticationException("Authentication failed")

    orig_connect = FakeTransport.connect
    FakeTransport.connect = auth_fail
    try:
        backend = _backend()
        assert backend.connect() is False
        assert _total_connect_calls() == 1, "bad credentials must not retry"
        assert no_sleep == [], "no backoff sleeps after an auth failure"
        assert backend.connected is False
    finally:
        FakeTransport.connect = orig_connect


def test_connect_bad_host_key_fails_immediately(fake_paramiko, no_sleep, fake_socket):
    def bad_key(self, username=None, password=None, pkey=None):
        self.connect_calls += 1
        raise FakeBadHostKeyException("host key mismatch")

    orig_connect = FakeTransport.connect
    FakeTransport.connect = bad_key
    try:
        backend = _backend()
        assert backend.connect() is False
        assert _total_connect_calls() == 1
        assert no_sleep == []
    finally:
        FakeTransport.connect = orig_connect


def test_connect_refused_fails_immediately(fake_paramiko, no_sleep, monkeypatch):
    """TCP refusal is an authoritative answer from a reachable peer — not a
    rate limit — so it must fail fast instead of retrying 3x (an unreachable
    remote would otherwise stall every connect caller ~3 OS timeouts)."""
    import socket as real_socket

    refused_calls = []

    def refused_connect(addr, timeout=None):
        refused_calls.append(addr)
        raise ConnectionRefusedError(10061, "actively refused")

    monkeypatch.setattr(ftp_sftp, "socket",
                        types.SimpleNamespace(create_connection=refused_connect,
                                              timeout=real_socket.timeout,
                                              error=real_socket.error,
                                              gaierror=real_socket.gaierror))
    backend = _backend()
    assert backend.connect() is False
    assert len(refused_calls) == 1, "refused connect must not retry"
    assert no_sleep == [], "no backoff sleeps after a TCP refusal"
    assert backend.connected is False


def test_connect_bad_host_name_fails_immediately(fake_paramiko, no_sleep, monkeypatch):
    """DNS failure (socket.gaierror) means a bad host name — the spec's
    'bad host' case: fail immediately, no retry."""
    import socket as real_socket

    gai_calls = []

    def gai_connect(addr, timeout=None):
        gai_calls.append(addr)
        raise socket.gaierror(11001, "getaddrinfo failed")

    monkeypatch.setattr(ftp_sftp, "socket",
                        types.SimpleNamespace(create_connection=gai_connect,
                                              timeout=real_socket.timeout,
                                              error=real_socket.error,
                                              gaierror=real_socket.gaierror))
    backend = _backend()
    assert backend.connect() is False
    assert len(gai_calls) == 1, "bad host name must not retry"
    assert no_sleep == []
