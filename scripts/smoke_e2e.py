"""Full verification pass (Plan 05 checklist) — real CLI end-to-end.

Runs the actual `backupme` CLI (via python -m backupme.main) against a
synthetic cPanel-format archive in a scratch workspace:
  gen1 ingest -> idempotent re-run -> gen2 (1 changed file, near-zero new
  bytes) -> ingest-status --json -> restore --retar -> cold-export -> purge
  -> history -> backend onboard/rebalance/drain -> web /ingest + APIs.
"""
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PY = sys.executable
FAILURES = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def run(args, cwd, env):
    r = subprocess.run([PY, "-m", "backupme.main"] + args,
                       cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    return r


def make_archive(path, index_content):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        members = [
            ("homedir/index.php", index_content),
            ("homedir/config.php", b"<?php 1234567890"),
            ("mysql/db.sql", b"INSERT INTO x VALUES (1);"),
        ]
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        # traversal member must be skipped
        info = tarfile.TarInfo(name="../evil.txt")
        info.size = 5
        tar.addfile(info, io.BytesIO(b"EVIL!"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


def main():
    ws = Path(tempfile.mkdtemp(prefix="backupme_verify_"))
    env = dict(os.environ)
    env["BACKUPME_CONFIG"] = str(ws / "config.yaml")
    (ws / "config.yaml").write_text(
        "backends:\n"
        "  local:\n"
        "  - name: pool_a\n"
        f"    path: {(ws / 'storage_a').as_posix()}\n"
        "    priority: 100\n"
        "  - name: pool_b\n"
        f"    path: {(ws / 'storage_b').as_posix()}\n"
        "    priority: 90\n"
        "encryption:\n"
        "  enabled: false\n"
        "cpanel_ingest:\n"
        "  enabled: true\n"
        f"  drop_root: {(ws / 'drop').as_posix()}\n"
        f"  staging_dir: {(ws / 'staging').as_posix()}\n"
    )
    print(f"workspace: {ws}\n")

    # 1. gen1 ingest via real CLI
    drop = ws / "drop" / "acme"
    make_archive(drop / "cpmove-acme.tar.gz", b"<?php echo 1;")
    r = run(["ingest-cpanel", str(ws / "drop")], ws, env)
    check("gen1 ingest exit 0", r.returncode == 0, r.stderr[-300:])

    # 2. dry-run --json parses
    r = run(["ingest-cpanel", str(ws / "drop"), "--dry-run", "--json"], ws, env)
    ok = r.returncode == 0
    data = json.loads(r.stdout) if ok else {}
    check("dry-run --json parses, 0 archives left", ok and data.get("archives") == 0)

    # 3. idempotent re-run: exact same bytes (copied back from .done) -> skipped
    done_arch = ws / "drop" / ".done" / "acme" / "cpmove-acme.tar.gz"
    check("gen1 archive moved to .done", done_arch.exists())
    import shutil
    shutil.copy2(done_arch, drop / "cpmove-acme.tar.gz")
    r = run(["ingest-cpanel", str(ws / "drop"), "--json"], ws, env)
    ok = r.returncode == 0
    res = json.loads(r.stdout) if ok else {}
    check("idempotent re-run is skipped",
          ok and res.get("results", [{}])[0].get("status") == "skipped",
          r.stdout[:200])

    # 4. gen2: change one file -> dedup hits, near-zero new bytes
    r = run(["stats", "--json"], ws, env)
    s1 = json.loads(r.stdout)
    make_archive(drop / "cpmove-acme.tar.gz", b"<?php echo 2; // changed")
    r = run(["ingest-cpanel", str(ws / "drop"), "--json"], ws, env)
    ok = r.returncode == 0
    res = json.loads(r.stdout) if ok else {}
    dupe = res.get("results", [{}])[0].get("dupe", 0)
    check("gen2 ingest exit 0", ok, r.stderr[-300:])
    check("gen2 dedup hits == 2", dupe == 2, f"dupe={dupe}")
    r = run(["stats", "--json"], ws, env)
    s2 = json.loads(r.stdout)
    # Stored-byte growth is a small absolute constant (one new bundle tar +
    # manifest) regardless of vault size; for this tiny vault use an absolute cap.
    growth = s2["stored_bytes"] - s1["stored_bytes"]
    check("gen2 near-zero new stored bytes",
          0 < growth < 500,
          f"growth={growth}, logical={s2['raw_bytes']}")

    # 5. ingest-status --json
    r = run(["ingest-status", "--json"], ws, env)
    ok = r.returncode == 0
    st = json.loads(r.stdout) if ok else {}
    check("ingest-status --json has accounts/generations/dedup/storage",
          ok and st.get("accounts") and st.get("generations") and "dedup" in st and "storage" in st)

    # 6. restore --retar
    r = run(["restore-cpanel", "acme", "1", "--retar", str(ws / "acme.tar.gz")], ws, env)
    check("restore-cpanel exit 0", r.returncode == 0, r.stderr[-300:])
    import tarfile as tf
    with tf.open(ws / "acme.tar.gz", "r:gz") as t:
        names = sorted(t.getnames())
    check("retar valid with original member names",
          names == ["homedir/config.php", "homedir/index.php", "mysql/db.sql"], str(names))

    # 7. cold-export -> purge -> history
    r = run(["db", "cold-export"], ws, env)
    check("db cold-export exit 0", r.returncode == 0, r.stderr[-200:])
    r = run(["db", "history", "SELECT COUNT(*) AS n FROM files", "--json"], ws, env)
    ok = r.returncode == 0
    hist = json.loads(r.stdout) if ok else {}
    check("db history returns rows", ok and hist.get("rows", [{}])[0].get("n", 0) >= 6)
    r = run(["db", "purge"], ws, env)
    check("db purge exit 0", r.returncode == 0, r.stderr[-200:])

    # 8. backend onboard (pool_b) + drain (pool_a)
    r = run(["backend", "onboard", "pool_b", "--rebalance", "--json"], ws, env)
    ok = r.returncode == 0
    ob = json.loads(r.stdout) if ok else {}
    check("backend onboard pool_b --rebalance copies blobs",
          ok and ob.get("rebalance", {}).get("copied", 0) >= 1, r.stdout[-200:] + r.stderr[-200:])
    r = run(["backend", "drain", "pool_a", "--json"], ws, env)
    ok = r.returncode == 0
    dr = json.loads(r.stdout) if ok else {}
    check("backend drain pool_a leaves nothing only-here",
          ok and dr.get("remaining_only_here_blobs") == 0)

    # 9. restore the latest generation after drain (data must live on pool_b)
    r = run(["ingest-status", "--json", "--account", "acme"], ws, env)
    gens = json.loads(r.stdout).get("generations", []) if r.returncode == 0 else []
    last_gen = max((g["generation"] for g in gens), default=0)
    check("latest generation is 2 (skip-run created nothing)", last_gen == 2,
          f"gens={[g['generation'] for g in gens]}")
    r = run(["restore-cpanel", "acme", str(last_gen), "--out", str(ws / "verify_restore")], ws, env)
    check("restore after drain exit 0", r.returncode == 0, r.stderr[-300:])
    idx = ws / "verify_restore" / "acme" / "homedir" / "index.php"
    got = idx.read_bytes() if idx.exists() else None
    check("restored latest-gen content correct",
          got == b"<?php echo 2; // changed",
          f"exists={idx.exists()} content={got!r}")

    # 10. web app: /ingest page + APIs
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.chdir(ws)
    from backupme.web.app import create_app
    from backupme.config import load_config
    client = TestClient(create_app(load_config()))
    page = client.get("/ingest")
    check("GET /ingest renders", page.status_code == 200 and "cPanel Ingest" in page.text)
    status = client.get("/api/ingest/status")
    check("GET /api/ingest/status ok",
          status.status_code == 200 and status.json()["sqlite"]["files"] >= 6)
    usage = client.get("/api/backends/usage")
    rows = {b["name"]: b for b in usage.json()["backends"]}
    check("GET /api/backends/usage shows pool_b holding data",
          usage.status_code == 200 and rows.get("pool_b", {}).get("stored_bytes", 0) > 0)

    print()
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
