import sys
import os
import hashlib
import json

def get_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def get_fingerprint(path):
    st = os.stat(path)
    size = st.st_size
    mtime = st.st_mtime
    
    h = hashlib.sha256()
    with open(path, "rb") as f:
        head = f.read(4096)
        h.update(head)
        
        if size > 4096:
            try:
                f.seek(size - 4096)
                tail = f.read(4096)
                h.update(tail)
            except Exception:
                pass
    return {"size": size, "mtime": mtime, "head_tail_sha256": h.hexdigest()}

def list_files(base_path):
    result = []
    for root, dirs, files in os.walk(base_path):
        for file in files:
            full_path = os.path.join(root, file)
            # Skip the agent script itself
            if file == ".backupme_agent.py":
                continue
            try:
                st = os.stat(full_path)
                rel = os.path.relpath(full_path, base_path).replace("\\", "/")
                result.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime})
            except Exception:
                continue
    return result

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No command provided"}))
        return
        
    cmd = sys.argv[1]
    try:
        if cmd == "list":
            path = sys.argv[2]
            print(json.dumps(list_files(path)))
        elif cmd == "hash":
            path = sys.argv[2]
            print(json.dumps({"hash": get_hash(path)}))
        elif cmd == "fingerprint":
            path = sys.argv[2]
            print(json.dumps(get_fingerprint(path)))
        elif cmd == "exists":
            path = sys.argv[2]
            print(json.dumps({"exists": os.path.isfile(path)}))
        elif cmd == "free":
            path = sys.argv[2]
            try:
                stat = os.statvfs(path)
                print(json.dumps({
                    "free": stat.f_bavail * stat.f_frsize,
                    "total": stat.f_blocks * stat.f_frsize
                }))
            except AttributeError:
                # Windows fallback (in case agent runs on a Windows SSH server)
                import ctypes
                free_bytes = ctypes.c_ulonglong(0)
                total_bytes = ctypes.c_ulonglong(0)
                ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                    ctypes.c_wchar_p(path), None, ctypes.byref(total_bytes), ctypes.byref(free_bytes)
                )
                print(json.dumps({
                    "free": free_bytes.value,
                    "total": total_bytes.value
                }))
        else:
            print(json.dumps({"error": f"Unknown command: {cmd}"}))
    except Exception as e:
        print(json.dumps({"error": str(e)}))

if __name__ == "__main__":
    main()
