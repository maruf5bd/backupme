import hashlib
import json
import re

import httpx
import pytest

from backupme.backends import BACKEND_MAP, get_backend, invalidate_cache
from backupme.backends.ninedrive import NineDriveBackend, NineDriveError


def _parse_multipart(content: bytes, boundary: bytes):
    parts = content.split(b"--" + boundary)
    fields = {}
    file_data = None
    for part in parts:
        if b'name="filesMeta"' in part:
            _, _, body = part.partition(b"\r\n\r\n")
            fields["filesMeta"] = json.loads(body.rstrip(b"\r\n").decode())
        elif b'name="file-0"' in part:
            _, _, body = part.partition(b"\r\n\r\n")
            if body.endswith(b"\r\n"):
                body = body[:-2]
            file_data = body
    return fields, file_data


def make_9drive(state=None):
    state = state or {}
    state.setdefault("files", {})
    state.setdefault("folders", {})
    state.setdefault("next_id", 1)
    state.setdefault("logins", 0)
    state.setdefault("refreshes", 0)
    state.setdefault("me_calls", 0)
    state.setdefault("uploads", [])
    state.setdefault("created_folders", [])

    def ser(f):
        return {k: v for k, v in f.items() if k != "data"}

    def jwt_ok(request):
        return request.headers.get("authorization") in ("Bearer acc", "Bearer acc2")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        auth = request.headers.get("authorization", "")

        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})

        if path == "/auth/login" and method == "POST":
            state["logins"] += 1
            if json.loads(request.content).get("password") == "bad":
                return httpx.Response(401, json={"code": "AUTH_INVALID_CREDENTIALS"})
            return httpx.Response(200, json={
                "accessToken": "acc", "refreshToken": "ref",
                "user": {"id": "u1", "name": "U", "email": "u@e.com"},
            })

        if path == "/auth/refresh" and method == "POST":
            state["refreshes"] += 1
            if json.loads(request.content).get("refreshToken") != "valid_refresh":
                return httpx.Response(401, json={"code": "AUTH_SESSION_EXPIRED"})
            return httpx.Response(200, json={"accessToken": "acc2"})

        if path == "/auth/me":
            state["me_calls"] += 1
            return httpx.Response(200, json={"user": {"id": "u1", "name": "U", "email": "u@e.com"}})

        if path == "/folders":
            if not jwt_ok(request):
                return httpx.Response(401, json={"code": "AUTH_REQUIRED"})
            if method == "GET":
                return httpx.Response(200, json={"folders": [ser(f) for f in state["folders"].values()]})
            if method == "POST":
                fid = f"folder-{state['next_id']}"
                state["next_id"] += 1
                folder = {
                    "id": fid, "name": json.loads(request.content).get("name"), "parentId": None,
                    "providerFolderId": None, "connectedAccountId": None,
                    "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
                }
                state["folders"][fid] = folder
                state["created_folders"].append(folder["name"])
                return httpx.Response(201, json={"folder": folder})

        if path == "/storage/summary":
            if not jwt_ok(request):
                return httpx.Response(401, json={"code": "AUTH_REQUIRED"})
            return httpx.Response(200, json={
                "totalBytes": "1000000", "usedBytes": "100000",
                "availableBytes": "900000", "accounts": [],
            })

        if path == "/api/v1/uploads" and method == "POST":
            if auth not in ("Bearer 9d_live_test", "Bearer acc", "Bearer acc2"):
                return httpx.Response(401, json={"code": "API_KEY_REQUIRED"})
            if state.get("upload_fail"):
                return httpx.Response(400, json={
                    "code": "NO_ACCOUNT_WITH_ENOUGH_SPACE",
                    "message": "No connected storage account has enough space.",
                    "failed": [],
                })
            boundary = re.search(r"boundary=([^;]+)", request.headers.get("content-type", "")).group(1).encode()
            fields, file_data = _parse_multipart(request.content, boundary)
            meta = (fields.get("filesMeta") or [{}])[0]
            fid = f"file-{state['next_id']}"
            state["next_id"] += 1
            entry = {
                "id": fid, "name": meta.get("fileName"),
                "sizeBytes": str(len(file_data or b"")),
                "mimeType": "application/octet-stream", "provider": "s3",
                "providerFileId": "obj", "status": "active",
                "folderId": meta.get("folderId"), "connectedAccountId": "acc1",
                "createdAt": "2026-01-01T00:00:00Z", "data": file_data or b"",
            }
            state["files"][fid] = entry
            state["uploads"].append({
                "meta": meta, "content": request.content,
                "content_type": request.headers.get("content-type", ""),
                "authorization": auth,
            })
            return httpx.Response(201, json={"files": [ser(entry)], "failed": []})

        if path == "/files" and method == "GET":
            if not jwt_ok(request):
                return httpx.Response(401, json={"code": "AUTH_REQUIRED"})
            if state.get("files_401_once") and not state.get("files_401_used"):
                state["files_401_used"] = True
                return httpx.Response(401, json={"code": "AUTH_INVALID_TOKEN"})
            folder_id = request.url.params.get("folderId")
            q = request.url.params.get("q")
            out = []
            for f in state["files"].values():
                if f["status"] != "active":
                    continue
                if folder_id and f.get("folderId") != folder_id:
                    continue
                if q and q not in f["name"]:
                    continue
                out.append(ser(f))
            return httpx.Response(200, json={"files": out})

        m = re.match(r"^/files/([^/]+)/download$", path)
        if m and method == "GET":
            if not jwt_ok(request):
                return httpx.Response(401, json={"code": "AUTH_REQUIRED"})
            f = state["files"].get(m.group(1))
            if not f or f["status"] != "active":
                return httpx.Response(404, json={"code": "FILES_NOT_FOUND"})
            rng = request.headers.get("range")
            data = f["data"]
            if rng and rng.startswith("bytes="):
                s, e = rng[6:].split("-")
                s = int(s)
                e = int(e) if e else len(data) - 1
                return httpx.Response(
                    206, content=data[s:e + 1],
                    headers={"Content-Range": f"bytes {s}-{e}/{len(data)}"},
                )
            return httpx.Response(200, content=data)

        m = re.match(r"^/files/([^/]+)$", path)
        if m and method == "DELETE":
            if not jwt_ok(request):
                return httpx.Response(401, json={"code": "AUTH_REQUIRED"})
            f = state["files"].get(m.group(1))
            if not f:
                return httpx.Response(404, json={"code": "FILES_NOT_FOUND"})
            f["status"] = "deleted"
            return httpx.Response(200, json={"status": "ok"})

        if path == "/files/batch/permanent" and method == "POST":
            if not jwt_ok(request):
                return httpx.Response(401, json={"code": "AUTH_REQUIRED"})
            for fid in json.loads(request.content).get("fileIds", []):
                state["files"].pop(fid, None)
            return httpx.Response(200, json={"status": "ok", "deleted": 1, "failed": []})

        return httpx.Response(404, json={"code": "NOT_FOUND"})

    return handler, state


@pytest.fixture()
def nd_server():
    handler, state = make_9drive()
    backend = NineDriveBackend("nd", {
        "base_url": "http://9drive.test",
        "api_key": "9d_live_test",
        "email": "u@e.com",
        "password": "pw",
    })
    backend._session = httpx.Client(base_url="http://9drive.test", transport=httpx.MockTransport(handler))
    return backend, state


def test_factory_registration():
    assert BACKEND_MAP["ninedrive"] is NineDriveBackend


def test_get_backend_connects(monkeypatch):
    handler, state = make_9drive()
    monkeypatch.setattr(
        NineDriveBackend, "_get_session",
        lambda self: httpx.Client(base_url="http://9drive.test", transport=httpx.MockTransport(handler)),
    )
    cfg = {"ninedrive": [{
        "name": "nd1", "base_url": "http://9drive.test",
        "api_key": "9d_live_test", "email": "u@e.com", "password": "pw",
    }]}
    bk = get_backend("nd1", cfg)
    assert bk is not None
    assert bk.connected
    assert bk.type == "ninedrive"
    invalidate_cache()


def test_connect_login_and_folder_creation(nd_server):
    bk, state = nd_server
    assert bk.connect() is True
    assert state["logins"] == 1
    assert state["created_folders"] == ["backupme"]
    assert bk._folder_id is not None


def test_connect_refresh_token(nd_server):
    handler, state = make_9drive()
    bk = NineDriveBackend("nd", {
        "base_url": "http://9drive.test",
        "refresh_token": "valid_refresh",
    })
    bk._session = httpx.Client(base_url="http://9drive.test", transport=httpx.MockTransport(handler))
    assert bk.connect() is True
    assert state["refreshes"] == 1
    assert state["logins"] == 0


def test_connect_without_creds_fails():
    handler, state = make_9drive()
    bk = NineDriveBackend("nd", {"base_url": "http://9drive.test"})
    bk._session = httpx.Client(base_url="http://9drive.test", transport=httpx.MockTransport(handler))
    assert bk.connect() is False


def test_connect_api_key_only_succeeds():
    handler, state = make_9drive()
    bk = NineDriveBackend("nd", {
        "base_url": "http://9drive.test", "api_key": "9d_live_test",
    })
    bk._session = httpx.Client(base_url="http://9drive.test", transport=httpx.MockTransport(handler))
    assert bk.connect() is True


def test_upload_download_roundtrip(nd_server, tmp_path):
    bk, state = nd_server
    assert bk.connect()
    src = tmp_path / "blob.bin"
    data = b"ninedrive-blob-payload" * 100
    src.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()

    assert bk.upload(str(src), "objects/aa/bb/blob1") == sha
    assert bk.hash("objects/aa/bb/blob1") == sha
    assert bk.exists("objects/aa/bb/blob1")

    dest = tmp_path / "out" / "blob1"
    bk.download("objects/aa/bb/blob1", str(dest))
    assert dest.read_bytes() == data


def test_upload_multipart_format(nd_server, tmp_path):
    bk, state = nd_server
    bk.connect()
    src = tmp_path / "blob.bin"
    src.write_bytes(b"x" * 1234)
    bk.upload(str(src), "objects/ab/cd/blob2")

    last = state["uploads"][-1]
    assert "multipart/form-data" in last["content_type"]
    assert last["authorization"] == "Bearer 9d_live_test"
    meta = last["meta"]
    assert meta["fileName"] == "objects/ab/cd/blob2"
    assert meta["sizeBytes"] == "1234"
    assert meta["mimeType"] == "application/octet-stream"
    assert meta["folderId"] == bk._folder_id
    content = last["content"]
    assert content.index(b'name="filesMeta"') < content.index(b'name="file-0"')


def test_upload_server_failure_raises(nd_server, tmp_path):
    bk, state = nd_server
    bk.connect()
    state["upload_fail"] = True
    src = tmp_path / "blob.bin"
    src.write_bytes(b"data")
    with pytest.raises(NineDriveError):
        bk.upload(str(src), "objects/aa/bb/x")


def test_list_and_walk_prefix_filter(nd_server, tmp_path):
    bk, state = nd_server
    bk.connect()
    for name in ("objects/aa/bb/h1", "objects/ab/cd/h2", "manifests/xx/yy/m1"):
        p = tmp_path / "f"
        p.write_bytes(b"z" * 10)
        bk.upload(str(p), name)

    all_files = bk.list_files()
    assert len(all_files) == 3
    objects = bk.list_files("objects")
    assert len(objects) == 2
    assert all(f["path"].startswith("objects/") for f in objects)
    assert bk.walk("objects/aa") == [f for f in objects if f["path"].startswith("objects/aa")]
    entry = all_files[0]
    assert set(("path", "size", "mtime")) <= set(entry.keys())
    assert entry["size"] == 10


def test_folder_isolation(nd_server, tmp_path):
    bk, state = nd_server
    bk.connect()
    src = tmp_path / "blob.bin"
    src.write_bytes(b"isolated")
    bk.upload(str(src), "objects/aa/bb/iso1")

    state["files"]["file-99"] = {
        "id": "file-99", "name": "objects/aa/bb/iso1", "sizeBytes": "8",
        "status": "active", "folderId": "folder-other", "createdAt": "2026-01-01T00:00:00Z",
        "data": b"other",
    }
    bk._index = None
    matches = [f for f in bk.list_files() if f["path"] == "objects/aa/bb/iso1"]
    assert len(matches) == 1


def test_delete_trash_then_permanent(nd_server, tmp_path):
    bk, state = nd_server
    bk.connect()
    src = tmp_path / "blob.bin"
    src.write_bytes(b"gone soon")
    bk.upload(str(src), "objects/aa/bb/gone1")
    assert bk.exists("objects/aa/bb/gone1")

    bk.delete("objects/aa/bb/gone1")
    assert state["files"] == {}
    assert not bk.exists("objects/aa/bb/gone1")
    bk.delete("objects/aa/bb/gone1")


def test_free_and_total_space(nd_server):
    bk, state = nd_server
    bk.connect()
    assert bk.free_space() == 900000
    assert bk.total_space() == 1000000


def test_jwt_401_auto_retry_via_refresh():
    handler, state = make_9drive()
    state["files_401_once"] = True
    bk = NineDriveBackend("nd", {
        "base_url": "http://9drive.test",
        "api_key": "9d_live_test",
        "refresh_token": "valid_refresh",
    })
    bk._session = httpx.Client(base_url="http://9drive.test", transport=httpx.MockTransport(handler))
    assert bk.connect()
    assert state["refreshes"] == 1
    assert bk.list_files() == []
    assert state["refreshes"] == 2
    assert state["logins"] == 0


def test_fingerprint_uses_range_requests(nd_server, tmp_path):
    bk, state = nd_server
    bk.connect()
    data = bytes(range(256)) * 40
    src = tmp_path / "blob.bin"
    src.write_bytes(data)
    path = "objects/aa/bb/fp1"
    bk.upload(str(src), path)

    fp = bk.fingerprint(path)
    assert fp["size"] == len(data)
    expected = hashlib.sha256(data[:4096] + data[-4096:]).hexdigest()
    assert fp["head_tail_sha256"] == expected
    assert fp["mtime"]


def test_api_key_only_upload_mode(tmp_path):
    handler, state = make_9drive()
    bk = NineDriveBackend("nd", {
        "base_url": "http://9drive.test", "api_key": "9d_live_test",
    })
    bk._session = httpx.Client(base_url="http://9drive.test", transport=httpx.MockTransport(handler))
    assert bk.connect() is True

    src = tmp_path / "blob.bin"
    src.write_bytes(b"upload-only mode")
    sha = hashlib.sha256(b"upload-only mode").hexdigest()
    assert bk.upload(str(src), "objects/aa/bb/u1") == sha
    assert bk.hash("objects/aa/bb/u1") == sha
    assert bk.list_files() == []
    assert not bk.exists("objects/aa/bb/u1")
    assert bk.free_space() is None
    with pytest.raises(FileNotFoundError):
        bk.download("objects/aa/bb/u1", str(tmp_path / "out.bin"))


def test_hash_unknown_path_returns_none():
    handler, state = make_9drive()
    bk = NineDriveBackend("nd", {"base_url": "http://9drive.test", "api_key": "9d_live_test"})
    bk._session = httpx.Client(base_url="http://9drive.test", transport=httpx.MockTransport(handler))
    assert bk.hash("objects/aa/bb/never-uploaded") is None


def test_missing_base_url_connect_fails():
    bk = NineDriveBackend("nd", {})
    assert bk.connect() is False


def test_download_missing_file_raises(nd_server, tmp_path):
    bk, state = nd_server
    bk.connect()
    with pytest.raises(FileNotFoundError):
        bk.download("objects/aa/bb/nope", str(tmp_path / "out.bin"))


def test_bad_credentials_connect_fails():
    handler, state = make_9drive()
    bk = NineDriveBackend("nd", {
        "base_url": "http://9drive.test",
        "email": "u@e.com", "password": "bad",
    })
    bk._session = httpx.Client(base_url="http://9drive.test", transport=httpx.MockTransport(handler))
    assert bk.connect() is False
