"""Phase B UI/UX polish regression tests (presentation layer only).

Pins the visible contracts of the dashboard polish wave without touching
any route or backend behavior:

- base.html ships a favicon link (inline SVG data-URI);
- /files renders sortable headers with stable data-* attributes and
  humanized cell content (exact bytes in data-size, raw ISO stamps in
  data-ts/title), and falls back to the shared empty-state pattern when
  a search has zero results;
- /backends renders capacity usage bars when free/total space is
  available and consistent Connected / Not connected badges;
- active filter chips appear on /files only when query params are set.

Everything runs against a scratch DB (env/backends fixtures), never the
live database.
"""
import gc
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupme.web.app import create_app
from backupme.config import load_config

from tests.helpers import backup_bytes


# ---------------------------------------------------------------------------
# Fixtures (same isolated pattern as tests/test_web_p2.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _release_stray_db_locks():
    """TestClient portal threads leave thread-local sqlite connections
    behind; gc them so the next fresh-DB init never hits a Windows lock."""
    yield
    gc.collect()


@pytest.fixture()
def app(env, backends):
    return create_app(load_config())


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _seed_file(env, backends, rel="polish/note.txt", data=b"polish seed bytes"):
    return backup_bytes(Path(env), rel, data, backends)


# ---------------------------------------------------------------------------
# base.html — favicon
# ---------------------------------------------------------------------------

def test_base_renders_favicon_link(client):
    r = client.get("/")
    assert r.status_code == 200, r.text[:300]
    assert 'rel="icon"' in r.text, "base.html must ship a favicon link"
    assert "data:image/svg+xml" in r.text, "favicon should be the inline SVG mark"


# ---------------------------------------------------------------------------
# /files — sortable headers + humanized cells + empty state
# ---------------------------------------------------------------------------

def test_files_page_renders_sortable_headers_and_humanized_cells(client, env, backends):
    _seed_file(env, backends, "polish/sortme.txt", b"x" * 2048)
    r = client.get("/files")
    assert r.status_code == 200, r.text[:300]
    body = r.text

    # Sortable headers expose stable data-* attributes for the JS helper.
    assert 'data-sort="path"' in body
    assert 'data-sort="size"' in body
    assert 'data-sort="date"' in body
    assert body.count('class="sortable"') >= 3

    # Size cells carry the exact bytes in data-size; the visible text is
    # humanized (2048 -> "2.0 KB", never a raw "2048" alone).
    assert 'data-size="2048"' in body
    assert "2.0 KB" in body

    # Timestamp cells carry the raw stored value in data-ts (sort/hover
    # source) instead of showing the raw ISO in the cell.
    assert "data-ts=" in body


def test_files_page_empty_state_on_zero_results(client):
    r = client.get("/files", params={"q": "definitely-no-such-file-xyz"})
    assert r.status_code == 200, r.text[:300]
    assert "empty-state" in r.text, "zero-result searches must render the shared empty-state pattern"
    assert "No files found" in r.text


# ---------------------------------------------------------------------------
# /files — active filter chips
# ---------------------------------------------------------------------------

def test_files_filter_chips_appear_only_with_params(client):
    # Plain listing: no chips row at all.
    r = client.get("/files")
    assert r.status_code == 200
    assert 'id="active-filter-chips"' not in r.text

    # Query chip.
    r = client.get("/files", params={"q": "polish"})
    assert r.status_code == 200
    assert 'id="active-filter-chips"' in r.text
    assert "query: polish" in r.text

    # Backend chip.
    r = client.get("/files", params={"backend": "test_local"})
    assert "backend: test_local" in r.text

    # Source chip.
    r = client.get("/files", params={"source": "cpanel/"})
    assert "source: cpanel/" in r.text


def test_files_filter_chips_have_clear_links(client):
    r = client.get("/files", params={"q": "polish", "backend": "test_local"})
    assert r.status_code == 200
    body = r.text
    assert "chip-clear" in body, "each chip needs a clear button"
    # Clearing the query chip keeps the backend filter (and vice versa).
    assert "/files?backend=test_local" in body
    assert "/files?q=polish" in body
    # Clear-all returns to the bare listing.
    assert 'href="/files"' in body


# ---------------------------------------------------------------------------
# /backends — usage bars + connection badges
# ---------------------------------------------------------------------------

def _write_two_backend_config(env, broken_path: Path, ok_path: Path):
    """One healthy local backend and one whose path is an existing FILE
    (connect() must fail -> listed as not connected)."""
    (Path(env) / "config.yaml").write_text(
        "backends:\n"
        "  local:\n"
        "  - name: polish_ok\n"
        f"    path: {ok_path.as_posix()}\n"
        "    priority: 100\n"
        "  - name: polish_broken\n"
        f"    path: {broken_path.as_posix()}\n"
        "    priority: 50\n"
        "encryption:\n"
        "  enabled: false\n"
    )


def test_backends_page_usage_bar_and_connection_badges(env):
    ok_dir = Path(env) / "polish_storage"
    ok_dir.mkdir(parents=True, exist_ok=True)
    broken_file = Path(env) / "polish_not_a_dir"
    broken_file.write_text("a file where a backend root should be")
    _write_two_backend_config(env, broken_file, ok_dir)

    from backupme.backends import invalidate_cache
    invalidate_cache()
    client = TestClient(create_app(load_config()), raise_server_exceptions=False)
    try:
        r = client.get("/backends")
        assert r.status_code == 200, r.text[:300]
        body = r.text

        # Usage bar rendered for the backend with real free/total numbers.
        assert "data-usage-bar" in body, (
            "backends with known free/total space must render a capacity usage bar")

        # Consistent connection badges (no bare Online/Offline text).
        assert "Connected" in body
        assert "Not connected" in body, (
            "an unreachable backend root must render the not-connected badge")
        assert ">Online<" not in body and ">Offline<" not in body
    finally:
        # Drop the cached backend instances so later tests re-read their
        # own config (the pool is keyed by name and would otherwise keep
        # the broken-path instance alive).
        from backupme.web.routes import common as routes_common
        routes_common._backend_pool.clear()
        invalidate_cache()


# ---------------------------------------------------------------------------
# Sort helper JS ships intact (header-click wiring the tests cannot execute)
# ---------------------------------------------------------------------------

def test_files_page_sort_helper_script_present(client, env, backends):
    _seed_file(env, backends, "polish/sortjs.txt", b"sort helper wiring")
    r = client.get("/files")
    assert r.status_code == 200
    body = r.text
    assert "applySort" in body, "header-click sort helper must ship on the page"
    assert "#files-table-head th.sortable" in body, "headers must be wired for click sorting"
    assert "sort-asc" in body and "sort-desc" in body, "indicator classes must be managed"
    # Date sorting goes through the raw data-ts stamps, not the displayed text.
    assert "dataset.ts" in body


# ---------------------------------------------------------------------------
# Shared timestamp decorator is available to every page (app.js)
# ---------------------------------------------------------------------------

def test_app_js_exposes_timestamp_humanizer():
    js_path = (Path(__file__).resolve().parents[1]
               / "backupme" / "web" / "static" / "js" / "app.js")
    text = js_path.read_text(encoding="utf-8")
    assert "compactStamp" in text
    assert "decorateTimestamps" in text
    assert re.search(r"decorateTimestamps\(document\)", text), \
        "the humanizer must run on page init so data-ts cells render compact"
