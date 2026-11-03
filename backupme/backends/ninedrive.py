import hashlib
import json
import threading
import time
from pathlib import Path

import httpx

from .base import StorageBackend


class NineDriveError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class NineDriveBackend(StorageBackend):
    _ACCESS_TTL = 900
    _INDEX_TTL = 60
    _RANGE_SIZE = 4096

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        self.base_url = (self.config.get("base_url") or "").rstrip("/")
        self.api_key = self.config.get("api_key") or ""
        self.email = self.config.get("email") or ""
        self.password = self.config.get("password") or ""
        self.refresh_token_cfg = self.config.get("refresh_token") or ""
        self.folder_name = self.config.get("folder", "backupme")
        self._session = None
        self._lock = threading.RLock()
        self._access_token = None
        self._refresh_token = self.refresh_token_cfg
        self._access_expires = 0.0
        self._folder_id = None
        self._index = None
        self._index_ts = 0.0
        self._hash_cache = {}

    @property
    def type(self) -> str:
        return "ninedrive"

    def _have_jwt_creds(self) -> bool:
        return bool(self._refresh_token or (self.email and self.password))

    def _get_session(self) -> httpx.Client:
        if self._session is None or self._session.is_closed:
            self._session = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(connect=15.0, read=300.0, write=300.0, pool=30.0),
            )
        return self._session

    def connect(self) -> bool:
        if not self.base_url:
            return False
        try:
            if self._get_session().get("/health").status_code != 200:
                return False
            if self._have_jwt_creds():
                self._require_jwt()
                self._ensure_folder()
            elif not self.api_key:
                return False
            self._connected = True
            return True
        except Exception:
            self._connected = False
            return False

    def disconnect(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
        self._connected = False

    def _refresh_or_login(self):
        if self._refresh_token:
            resp = self._get_session().post("/auth/refresh", json={"refreshToken": self._refresh_token})
            if resp.status_code == 200:
                token = resp.json().get("accessToken")
                if token:
                    self._access_token = token
                    self._access_expires = time.monotonic() + self._ACCESS_TTL - 60
                    return
        if self.email and self.password:
            resp = self._get_session().post(
                "/auth/login", json={"email": self.email, "password": self.password}
            )
            if resp.status_code == 200:
                data = resp.json()
                self._access_token = data.get("accessToken")
                self._refresh_token = data.get("refreshToken") or self._refresh_token
                self._access_expires = time.monotonic() + self._ACCESS_TTL - 60
                return
        raise NineDriveError(
            "9drive: authentication failed — configure api_key, email/password, or refresh_token"
        )

    def _require_jwt(self) -> str:
        if self._access_token and time.monotonic() < self._access_expires:
            return self._access_token
        with self._lock:
            if self._access_token and time.monotonic() < self._access_expires:
                return self._access_token
            self._refresh_or_login()
            return self._access_token

    def _err_message(self, resp: httpx.Response) -> str:
        try:
            data = resp.json()
            code = data.get("code") or ""
            msg = data.get("message") or ""
            if code or msg:
                return f"{code} {msg}".strip()
        except Exception:
            pass
        return resp.text[:200]

    def _request(self, method, path, *, jwt=False, use_api_key=False, retry=True, **kw):
        headers = dict(kw.pop("headers", None) or {})
        if use_api_key and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif jwt:
            headers["Authorization"] = f"Bearer {self._require_jwt()}"
        resp = self._get_session().request(method, path, headers=headers, **kw)
        if resp.status_code == 401 and jwt and retry:
            with self._lock:
                self._access_token = None
                self._access_expires = 0.0
            return self._request(method, path, jwt=True, use_api_key=False, retry=False, **kw)
        if resp.status_code >= 400:
            raise NineDriveError(f"9drive {method} {path}: {self._err_message(resp)}")
        return resp

    def _ensure_folder(self):
        if not self.folder_name:
            return
        resp = self._request("GET", "/folders", params={"all": "1"}, jwt=True)
        for f in resp.json().get("folders", []):
            if f.get("name") == self.folder_name:
                self._folder_id = f.get("id")
                return
        resp = self._request("POST", "/folders", json={"name": self.folder_name}, jwt=True)
        self._folder_id = (resp.json().get("folder") or {}).get("id")

    def _refresh_index(self, force=False):
        if not force and self._index is not None and time.monotonic() - self._index_ts < self._INDEX_TTL:
            return
        params = {}
        if self._folder_id:
            params["folderId"] = self._folder_id
        resp = self._request("GET", "/files", params=params, jwt=True)
        files = resp.json().get("files", [])
        self._index = {f["name"]: f for f in files if f.get("status") == "active"}
        self._index_ts = time.monotonic()

    def _index_stale(self):
        self._index = None

    def _search(self, name):
        params = {"q": name}
        if self._folder_id:
            params["folderId"] = self._folder_id
        try:
            resp = self._request("GET", "/files", params=params, jwt=True)
        except NineDriveError:
            return None
        for f in resp.json().get("files", []):
            if f.get("name") == name and f.get("status") == "active":
                return f
        return None

    def _resolve(self, remote_rel_path):
        with self._lock:
            if self._index is None or time.monotonic() - self._index_ts >= self._INDEX_TTL:
                try:
                    self._refresh_index(force=True)
                except NineDriveError:
                    self._index = None
            if self._index and remote_rel_path in self._index:
                return self._index[remote_rel_path].get("id")
        f = self._search(remote_rel_path)
        return f.get("id") if f else None

    def upload(self, local_path: str, remote_rel_path: str) -> str:
        sha = self._sha256_file(local_path)
        meta = {
            "fieldName": "file-0",
            "fileName": remote_rel_path,
            "mimeType": "application/octet-stream",
            "sizeBytes": str(Path(local_path).stat().st_size),
        }
        if self._folder_id:
            meta["folderId"] = self._folder_id
        with open(local_path, "rb") as f:
            resp = self._request(
                "POST",
                "/api/v1/uploads",
                use_api_key=bool(self.api_key),
                jwt=not bool(self.api_key),
                data={"filesMeta": json.dumps([meta])},
                files={"file-0": (remote_rel_path, f, "application/octet-stream")},
            )
        body = resp.json()
        completed = body.get("files") or ([body["file"]] if body.get("file") else [])
        failed = body.get("failed") or []
        if not completed or failed:
            detail = "; ".join(
                f"{item.get('fileName', '?')}: {item.get('code', '')} {item.get('message', '')}".strip()
                for item in failed
            )
            raise NineDriveError(f"9drive upload failed for {remote_rel_path}: {detail or 'no file confirmed'}")
        self._hash_cache[remote_rel_path] = sha
        self._index_stale()
        return sha

    @staticmethod
    def _sha256_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def download(self, remote_rel_path: str, local_path: str):
        fid = self._resolve(remote_rel_path)
        if not fid:
            raise FileNotFoundError(f"9drive: {remote_rel_path} not found")
        out = Path(local_path)
        if str(out.parent):
            out.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            with self._get_session().stream(
                "GET",
                f"/files/{fid}/download",
                headers={"Authorization": f"Bearer {self._require_jwt()}"},
            ) as resp:
                if resp.status_code == 401 and attempt == 0:
                    with self._lock:
                        self._access_token = None
                        self._access_expires = 0.0
                    continue
                if resp.status_code >= 400:
                    raise NineDriveError(
                        f"9drive GET /files/{fid}/download: {self._err_message(resp)}"
                    )
                with open(out, "wb") as f:
                    for chunk in resp.iter_bytes(1024 * 1024):
                        f.write(chunk)
                return

    def delete(self, remote_rel_path: str):
        fid = self._resolve(remote_rel_path)
        if not fid:
            return
        self._request("DELETE", f"/files/{fid}", jwt=True)
        self._request("POST", "/files/batch/permanent", json={"fileIds": [fid]}, jwt=True)
        self._index_stale()

    def exists(self, remote_rel_path: str) -> bool:
        return self._resolve(remote_rel_path) is not None

    def list_files(self, prefix: str = "") -> list[dict]:
        try:
            self._refresh_index()
        except NineDriveError:
            return []
        idx = self._index or {}
        out = []
        for name, f in idx.items():
            if prefix and not name.startswith(prefix):
                continue
            out.append({
                "path": name,
                "size": int(f.get("sizeBytes") or 0),
                "mtime": f.get("createdAt", ""),
            })
        return out

    def walk(self, prefix: str = "") -> list[dict]:
        return self.list_files(prefix)

    def hash(self, remote_rel_path: str) -> str | None:
        return self._hash_cache.get(remote_rel_path)

    def _storage_summary(self):
        if not self._have_jwt_creds():
            return None
        try:
            return self._request("GET", "/storage/summary", jwt=True).json()
        except NineDriveError:
            return None

    def free_space(self) -> int | None:
        summary = self._storage_summary()
        if not summary:
            return None
        try:
            return int(summary.get("availableBytes") or 0)
        except (TypeError, ValueError):
            return None

    def total_space(self) -> int | None:
        summary = self._storage_summary()
        if not summary:
            return None
        try:
            return int(summary.get("totalBytes") or 0)
        except (TypeError, ValueError):
            return None

    def fingerprint(self, remote_rel_path: str) -> dict:
        f = None
        with self._lock:
            try:
                self._refresh_index(force=True)
            except NineDriveError:
                self._index = None
            if self._index and remote_rel_path in self._index:
                f = self._index[remote_rel_path]
        if f is None:
            f = self._search(remote_rel_path)
        f = f or {}
        size = int(f.get("sizeBytes") or 0)
        mtime = f.get("createdAt")
        fid = f.get("id")
        head_tail = ""
        if fid and size > 2 * self._RANGE_SIZE:
            head = self._range_download(fid, 0, self._RANGE_SIZE - 1)
            tail = self._range_download(fid, size - self._RANGE_SIZE, size - 1)
            if head is not None and tail is not None:
                head_tail = hashlib.sha256(head + tail).hexdigest()
        return {"size": size, "mtime": mtime, "head_tail_sha256": head_tail}

    def _range_download(self, fid, start, end):
        try:
            resp = self._request(
                "GET",
                f"/files/{fid}/download",
                jwt=True,
                headers={"Range": f"bytes={start}-{end}"},
            )
            content = resp.content
            if len(content) == end - start + 1:
                return content
        except Exception:
            pass
        return None

    def health(self) -> bool:
        try:
            return self._get_session().get("/health").status_code == 200
        except Exception:
            return False
