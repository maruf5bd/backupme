"""
FTP Streaming Ingest & Migration Pipeline for BackupMe
Processes remote FTP files ONE BY ONE:
1. Lists remote files matching pattern/directory.
2. Downloads File N into a local scratch staging folder.
3. Strictly validates local disk presence and integrity.
4. Uploads/deduplicates File N into BackupMe target backend(s).
5. Deletes File N from local staging scratch disk.
6. Upon successful backup confirmation, deletes File N from source FTP (if --delete-source).
"""
import ftplib
import os
import shutil
import tempfile
from typing import List, Dict, Any, Optional

from backupme.log import get_logger
from backupme.core.uploader import upload_file
from backupme.config import load_config
from backupme.backends import get_all_backends

log = get_logger("ftp_ingest")


class FTPFileProcessor:
    def __init__(
        self,
        host: str,
        port: int = 21,
        username: str = "anonymous",
        password: str = "",
        remote_dir: str = "/",
        delete_source: bool = True,
        target_backend_name: Optional[str] = None,
        scratch_dir: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.remote_dir = remote_dir.strip("/")
        self.delete_source = delete_source
        self.target_backend_name = target_backend_name
        self.scratch_dir = scratch_dir or tempfile.gettempdir()
        self.ftp: Optional[ftplib.FTP] = None

    def connect(self):
        log.info(f"Connecting to FTP {self.host}:{self.port} as {self.username}...")
        ftp = ftplib.FTP()
        ftp.connect(self.host, self.port, timeout=30)
        ftp.login(self.username, self.password)
        if self.remote_dir:
            ftp.cwd(self.remote_dir)
        self.ftp = ftp
        log.info("FTP connected successfully.")

    def disconnect(self):
        if self.ftp:
            try:
                self.ftp.quit()
            except Exception:
                self.ftp.close()
            self.ftp = None

    def list_files(self) -> List[str]:
        if not self.ftp:
            raise RuntimeError("FTP not connected.")
        
        file_list = []
        try:
            # Use NLST or MLSD
            lines = self.ftp.nlst()
            for line in lines:
                name = os.path.basename(line)
                if name not in (".", "..") and not name.startswith("."):
                    file_list.append(name)
        except Exception as e:
            log.error(f"Error listing FTP files: {e}")
        return file_list

    def process_single_file(self, filename: str, config, backends: list) -> Dict[str, Any]:
        """
        Downloads one file locally, uploads it to BackupMe, deletes local staging,
        and deletes remote FTP file on success.
        """
        staging_dir = os.path.join(self.scratch_dir, "backupme_ftp_staging")
        os.makedirs(staging_dir, exist_ok=True)
        local_filepath = os.path.join(staging_dir, filename)

        result = {
            "file": filename,
            "status": "pending",
            "bytes": 0,
            "deleted_source": False,
            "error": None
        }

        # Step 1: Download File N locally
        try:
            log.info(f"[1/4] Downloading {filename} from FTP to local scratch: {local_filepath}...")
            with open(local_filepath, "wb") as f_out:
                self.ftp.retrbinary(f"RETR {filename}", f_out.write)
            
            # Step 2: Strict disk verification
            if not os.path.exists(local_filepath) or os.path.getsize(local_filepath) == 0:
                raise RuntimeError(f"Downloaded file {filename} is missing or 0 bytes on local disk.")
            
            file_size = os.path.getsize(local_filepath)
            result["bytes"] = file_size
            log.info(f"[2/4] Verified on local disk: {filename} ({file_size} bytes)")

            # Step 3: Backup via BackupMe uploader
            log.info(f"[3/4] Processing through BackupMe pipeline to target storage...")
            source_root = f"ftp://{self.host}/{self.remote_dir}"
            
            # Select target backends
            if self.target_backend_name:
                targets = [b for b in backends if b.name == self.target_backend_name]
                if not targets:
                    raise RuntimeError(f"Target backend '{self.target_backend_name}' not found.")
            else:
                targets = backends  # auto: use all configured priority backends

            up_res = upload_file(
                source_root=source_root,
                relative_path=filename,
                local_path=local_filepath,
                backends=targets,
                config=config,
            )

            if up_res.get("status") in ("ok", "dedup", "partial"):
                result["status"] = up_res.get("status")
                log.info(f"Backup succeeded: {filename} ({result['status']})")
            else:
                raise RuntimeError(f"Backup failed: {up_res.get('error', 'unknown error')}")

            # Step 4: Delete from remote source FTP (if requested)
            if self.delete_source:
                try:
                    self.ftp.delete(filename)
                    result["deleted_source"] = True
                    log.info(f"[4/4] Deleted {filename} from source FTP.")
                except Exception as del_err:
                    log.warning(f"Failed to delete {filename} from source FTP: {del_err}")

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            log.error(f"Failed processing {filename}: {e}")

        finally:
            # Always clean up local scratch file immediately to conserve disk space!
            if os.path.exists(local_filepath):
                try:
                    os.remove(local_filepath)
                    log.info(f"Cleaned up local staging file: {local_filepath}")
                except Exception as clean_err:
                    log.warning(f"Could not remove local temp file {local_filepath}: {clean_err}")

        return result

    def run_all(self) -> List[Dict[str, Any]]:
        config = load_config()
        backends = get_all_backends(config)

        self.connect()
        try:
            files = self.list_files()
            log.info(f"Found {len(files)} file(s) on FTP server {self.host}")
            
            summary = []
            for idx, fname in enumerate(files, 1):
                log.info(f"\n--- Processing [{idx}/{len(files)}] {fname} ---")
                res = self.process_single_file(fname, config, backends)
                summary.append(res)
            return summary
        finally:
            self.disconnect()
