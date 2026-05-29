from abc import ABC, abstractmethod


class StorageBackend(ABC):
    def __init__(self, name: str, config: dict | None = None):
        self._name = name
        self.config = config or {}
        self._connected = False

    @property
    def name(self) -> str:
        return self._name

    @property
    @abstractmethod
    def type(self) -> str:
        ...

    @abstractmethod
    def connect(self) -> bool:
        ...

    @abstractmethod
    def disconnect(self):
        ...

    @abstractmethod
    def upload(self, local_path: str, remote_rel_path: str) -> str:
        ...

    @abstractmethod
    def download(self, remote_rel_path: str, local_path: str):
        ...

    @abstractmethod
    def delete(self, remote_rel_path: str):
        ...

    @abstractmethod
    def exists(self, remote_rel_path: str) -> bool:
        ...

    @abstractmethod
    def list_files(self, prefix: str = "") -> list[dict]:
        ...

    @abstractmethod
    def free_space(self) -> int | None:
        ...

    @abstractmethod
    def total_space(self) -> int | None:
        ...

    def quota(self) -> int | None:
        return self.config.get("quota")

    @abstractmethod
    def hash(self, remote_rel_path: str) -> str:
        ...

    @abstractmethod
    def health(self) -> bool:
        ...

    @abstractmethod
    def fingerprint(self, remote_rel_path: str) -> dict:
        ...

    @abstractmethod
    def walk(self, prefix: str = "") -> list[dict]:
        ...

    def db_upload(self, local_db_path: str) -> str:
        return self.upload(local_db_path, "__backupme_db__/backupme.db")

    def db_download(self, remote_db_path: str, local_path: str):
        self.download(remote_db_path, local_path)

    def db_list(self, kind: str = "snapshots") -> list[str]:
        prefix = f"__backupme_db__/{kind}"
        files = self.list_files(prefix)
        return [f["path"] for f in files]

    def db_upload_chunk(self, local_path: str, remote_rel_path: str) -> str:
        return self.upload(local_path, remote_rel_path)

    @property
    def connected(self) -> bool:
        return self._connected