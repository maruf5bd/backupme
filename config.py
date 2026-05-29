import os
import yaml
from pathlib import Path


class Config:
    def __init__(self, path: str = None):
        self.path = Path(path or "config.yaml")
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"backups": [], "backends": {}, "chunking": {}, "verification": {}, "database": {}}
        with open(self.path) as f:
            raw = f.read()
        raw = self._subst_env(raw)
        return yaml.safe_load(raw) or {}

    @staticmethod
    def _subst_env(text: str) -> str:
        import re
        def repl(m):
            key = m.group(1)
            return os.environ.get(key, m.group(0))
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", repl, text)

    def backups(self) -> list:
        return self.data.get("backups", [])

    def backends(self) -> dict:
        raw = self.data.get("backends", {})
        flat = {}
        for btype, blist in raw.items():
            for b in blist:
                b["type"] = btype
                flat[b["name"]] = b
        return flat

    def backend_list(self) -> list[dict]:
        return list(self.backends().values())

    def backend_names(self) -> list[str]:
        return list(self.backends().keys())

    def chunking(self) -> dict:
        return {
            "enabled": True,
            "min_size": 1 * 1024 * 1024,
            "chunk_size": 64 * 1024 * 1024,
            "parallel_uploads": 4,
            **self.data.get("chunking", {}),
        }

    def verification(self) -> dict:
        return {
            "enabled": True,
            "retries": 3,
            "fallback_on_fail": True,
            **self.data.get("verification", {}),
        }

    def database(self) -> dict:
        return {
            "snapshots": {"interval": 3600, "retention": 168},
            "auto_backup": {"enabled": True, "backends": "all"},
            **self.data.get("database", {}),
        }
