"""Code patches: move code changes between machines (personal PC → work laptop → server) without touching the data.

    python tools/patch.py build [--wheels] [--note "what changed"]      → dist/llmwiki-patch-<version>.zip
    apply_patch.bat [INSTALL_DIR] [--dry-run] [--force] [--no-restart]    (inside the unzipped patch)
    python tools/patch.py status                                          → installed version + files changed locally
    python tools/patch.py rollback                                        → undo the last patch (code; the DB copy is kept)
    python tools/patch.py manifest                                        → record the installed code (install.bat/setup.bat run it)

A patch holds the code only: llmwiki/, the built frontend, tools/, docs/, tests/, devtools/, manage.py, requirements and
the .bat files. Never data*/ (database, settings.yaml, company_adapter.py, models, logs), .venv or dist/. Applying it:
 1. compares with MANIFEST.json of the install (what the last install/patch wrote) to find files changed locally on that
    machine; a file changed there AND in the patch is a conflict → stop (or --force; the backup keeps both)
 2. stops the portal (scheduled task LLMWiki, or the process serving the port), backs up the files it will replace and a
    copy of the database to backups/
 3. installs changed requirements (from the patch's wheelhouse with --wheels, else the install's wheelhouse/)
 4. writes the new files, deletes files the patch removed (only unchanged ones), runs migrate and doctor, writes the
    new MANIFEST.json, restarts the portal if it was running
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRS = ["llmwiki", "llmwiki_site", "devtools", "tools", "docs", "frontend", "tests"]
ROOT_FILES = ["manage.py", "requirements.txt", "requirements.lock", "requirements-dev.txt", "README.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md",
              ".gitignore",
              "run.bat", "doctor.bat", "llmwiki.bat", "backfill.bat", "setup.bat"]
FROM_TOOLS = ["install.bat", "install_service.bat"]          # the server install has these at its root
SKIP_DIRS = {"__pycache__", "node_modules", ".venv", ".git", ".pytest_cache", ".ruff_cache"}
SKIP_NAMES = {"company_adapter.py", "settings.local.yaml", ".env", "BUILD.json"}
MANIFEST = "MANIFEST.json"


def sha1(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_files(root: Path) -> dict[str, Path]:
    """rel path (with /) → file, for every code file of a checkout or an install."""
    out = {}
    for d in DIRS:
        base = root / d
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS and not x.startswith("data")]
            for fn in filenames:
                if fn in SKIP_NAMES or fn.endswith((".pyc", ".log")):
                    continue
                p = Path(dirpath) / fn
                out[p.relative_to(root).as_posix()] = p
    for f in ROOT_FILES:
        if (root / f).exists():
            out[f] = root / f
    for f in FROM_TOOLS:
        if (root / "tools" / f).exists():
            out[f] = root / "tools" / f
    return out


def version_of(files: dict[str, str]) -> str:
    h = hashlib.sha1("".join(f"{k}:{v}\n" for k, v in sorted(files.items())).encode()).hexdigest()[:8]
    return f"{datetime.now():%Y%m%d-%H%M}-{h}"


# ------------------------------------------------------------------ build
def build(a):
    files = code_files(ROOT)
    hashes = {k: sha1(p) for k, p in files.items()}
    ver = version_of(hashes)
    src_dir, dist_dir = ROOT / "frontend" / "src", ROOT / "frontend" / "dist"
    if src_dir.exists() and dist_dir.exists():
        newest_src = max((p.stat().st_mtime for p in src_dir.rglob("*") if p.is_file()), default=0)
        newest_dist = max((p.stat().st_mtime for p in dist_dir.rglob("*") if p.is_file()), default=0)
        if newest_src > newest_dist + 1:
            print("WARNING: frontend/src changed after the last build of frontend/dist. Run `npm run build` in frontend/ "
                  "(needs Node) or the patch ships the old portal pages.")
    out = Path(a.out or ROOT / "dist")
    out.mkdir(parents=True, exist_ok=True)
    top = f"llmwiki-patch-{ver}"
    zname = out / f"{top}.zip"
    build_info = {"version": ver, "built": datetime.now().isoformat(timespec="seconds"), "built_on": socket.gethostname(), "note": a.note or ""}
    meta = {**build_info, "files": hashes, "wheels": bool(a.wheels), "lock_sha1": hashes.get("requirements.lock", "")}
    with zipfile.ZipFile(zname, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, p in sorted(files.items()):
            z.write(p, f"{top}/files/{rel}")
        z.writestr(f"{top}/files/llmwiki/BUILD.json", json.dumps(build_info, indent=1))
        z.writestr(f"{top}/PATCH.json", json.dumps(meta, indent=1))
        z.write(ROOT / "tools" / "apply_patch.bat", f"{top}/apply_patch.bat")
        z.writestr(f"{top}/README.txt", README.format(ver=ver, note=a.note or "-", n=len(files)))
        if a.wheels:
            wh = out / "stage-wheels"
            shutil.rmtree(wh, ignore_errors=True)
            subprocess.run([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--platform", a.platform, "--python-version", a.python,
                            "--implementation", "cp", "--abi", f"cp{a.python.replace('.', '')}", "-r", str(ROOT / "requirements.lock"), "-d", str(wh)], check=True)
            for p in wh.iterdir():
                z.write(p, f"{top}/wheelhouse/{p.name}")
            shutil.rmtree(wh, ignore_errors=True)
    print(f"done: {zname} ({zname.stat().st_size / 1e6:.1f} MB, {len(files)} files, version {ver}). No data inside. "
          "Copy it to the other machine, unzip it INTO the llmwiki folder and run apply_patch.bat there.")


README = """LLM Wiki code patch {ver}
Note: {note}
Files: {n} (code only: no database, no settings, no company_adapter.py, no models)

To apply:
 1. Unzip this folder into the LLM Wiki folder (the one with manage.py and data\\).
 2. Run apply_patch.bat in it (or: apply_patch.bat D:\\path\\to\\llmwiki). Add --dry-run to only see what would change.
It stops the portal, backs up the replaced files and the database to backups\\, installs the code, runs the database
migrations and the doctor, and starts the portal again. Undo: .venv\\Scripts\\python tools\\patch.py rollback
"""


# ------------------------------------------------------------------ apply
def _data_dir(target: Path) -> Path:
    d = os.environ.get("LLMWIKI_DATA")
    return Path(d) if d else target / "data"


def _port(target: Path) -> int:
    try:
        import yaml
        y = yaml.safe_load((_data_dir(target) / "settings.yaml").read_text(encoding="utf-8")) or {}
        return int((y.get("server") or {}).get("port") or 8000)
    except Exception:
        return 8000


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _task_exists() -> bool:
    if os.name != "nt":
        return False
    return subprocess.run(["schtasks", "/Query", "/TN", "LLMWiki"], capture_output=True).returncode == 0


def stop_portal(target: Path) -> str:
    """Returns how it was running: 'task', 'process' or ''."""
    port = _port(target)
    how = ""
    if _task_exists():
        q = subprocess.run(["schtasks", "/Query", "/TN", "LLMWiki", "/FO", "LIST"], capture_output=True, text=True).stdout
        if "Running" in q:
            subprocess.run(["schtasks", "/End", "/TN", "LLMWiki"], capture_output=True)
            how = "task"
    if _listening(port) and os.name == "nt":
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
              "Where-Object { $_.CommandLine -like '*manage.py*serve*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        if r.stdout.strip():
            print(f"  stopped the portal process(es): {' '.join(r.stdout.split())}")
            how = how or "process"
    for _ in range(20):
        if not _listening(port):
            break
        import time
        time.sleep(0.5)
    if _listening(port):
        raise SystemExit(f"Port {port} is still in use: stop the portal (close run.bat) and run apply_patch.bat again.")
    return how


def _venv_python(target: Path) -> str:
    p = target / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(p) if p.exists() else sys.executable


def _run(target: Path, *args, check=True) -> int:
    env = {**os.environ, "HF_HUB_OFFLINE": "1"}
    env.setdefault("LLMWIKI_DATA", str(_data_dir(target)))
    print("  $", " ".join(args), flush=True)
    return subprocess.run([_venv_python(target), *args], cwd=str(target), env=env, check=check).returncode


def apply(a):
    src = Path(a.src).resolve()
    if src.is_file() and src.suffix == ".zip":
        tmp = src.parent / src.stem
        with zipfile.ZipFile(src) as z:
            z.extractall(src.parent)
        src = tmp
    meta = json.loads((src / "PATCH.json").read_text(encoding="utf-8"))
    target = Path(a.target or (a.target_pos if a.target_pos and not str(a.target_pos).startswith("-") else src.parent)).resolve()
    if not (target / "manage.py").exists():
        raise SystemExit(f"No LLM Wiki install in {target} (no manage.py). Give the folder: apply_patch.bat D:\\path\\to\\llmwiki")
    mf = target / MANIFEST
    installed = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else None
    new = meta["files"]
    print(f"Patch {meta['version']} ({meta.get('note') or 'no note'}) → {target}")
    print(f"Installed: {installed['version'] if installed else 'unknown (no MANIFEST.json yet: local changes cannot be detected)'}")
    writes, same, skipped_root, conflicts, local_kept = [], 0, [], [], []
    for rel, h in new.items():
        dst = target / rel
        if "/" not in rel and rel.endswith(".bat") and not dst.exists():
            skipped_root.append(rel)          # keep each machine's layout (repo checkout vs server install)
            continue
        cur = sha1(dst) if dst.exists() else None
        if cur == h:
            same += 1
            continue
        old = (installed or {}).get("files", {}).get(rel)
        if installed and old == h and cur is not None:
            local_kept.append(rel)            # changed on this machine only: the patch has the old version → keep it
            continue
        if installed and cur is not None and (old is None or cur != old):
            conflicts.append(rel)             # changed (or created) on this machine AND changed by the patch
        writes.append(rel)
    deletes = []
    for rel, old in ((installed or {}).get("files") or {}).items():
        if rel in new or not any(rel.startswith(d + "/") for d in DIRS):
            continue
        p = target / rel
        if not p.exists():
            continue
        if sha1(p) == old:
            deletes.append(rel)
        else:
            local_kept.append(rel)
    lock_changed = sha1(target / "requirements.lock") != new.get("requirements.lock") if (target / "requirements.lock").exists() else True
    print(f"  {len(writes)} files to write, {same} unchanged, {len(deletes)} to delete" + (f", {len(skipped_root)} root .bat not used here" if skipped_root else ""))
    print(f"  requirements: {'CHANGED' if lock_changed else 'same'}")
    for rel in conflicts:
        print(f"  CONFLICT: {rel} was changed on this machine and by the patch")
    for rel in local_kept:
        print(f"  kept as changed on this machine (the patch does not change it): {rel}")
    if a.dry_run:
        for rel in writes[:60]:
            print("   write", rel)
        for rel in deletes[:30]:
            print("   delete", rel)
        print("Dry run: nothing changed.")
        return
    if conflicts and not a.force:
        raise SystemExit("Stopped: files changed on this machine would be overwritten. Bring those changes into the patch's source "
                         "first, or run again with --force (the backup keeps this machine's version).")
    if not writes and not deletes and not lock_changed:
        print("Nothing to do: this install already has this code.")
        return
    how = "" if a.no_stop else stop_portal(target)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = target / "backups"
    bdir.mkdir(exist_ok=True)
    bzip = bdir / f"code-before-{meta['version']}-{ts}.zip"
    added = [r for r in writes if not (target / r).exists()]
    with zipfile.ZipFile(bzip, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in writes + deletes:
            if (target / rel).exists():
                z.write(target / rel, f"files/{rel}")
        if mf.exists():
            z.write(mf, MANIFEST)
        z.writestr("ROLLBACK.json", json.dumps({"patch": meta["version"], "added": added, "previous": (installed or {}).get("version")}, indent=1))
    print(f"  backup of the replaced code: {bzip}")
    db = _data_dir(target) / "llmwiki.sqlite3"
    if db.exists():
        dbb = bdir / f"db-before-{meta['version']}-{ts}.sqlite3"
        with sqlite3.connect(str(db)) as s_, sqlite3.connect(str(dbb)) as d_:
            s_.backup(d_)
        print(f"  backup of the database: {dbb}")
    if lock_changed:
        wh = src / "wheelhouse" if (src / "wheelhouse").exists() else target / "wheelhouse"
        cmd = ["-m", "pip", "install", "-r", str(src / "files" / "requirements.lock")]
        if wh.exists() and not a.online:
            cmd += ["--no-index", "--find-links", str(wh)]
        elif not a.online:
            raise SystemExit("The patch changes requirements.lock but there is no wheelhouse: build the patch with --wheels, "
                             "or run with --online on a machine with pip access. Nothing was changed (backups are in backups\\).")
        _run(target, *cmd)
    for rel in writes:
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "files" / rel, dst)
    if (src / "files" / "llmwiki" / "BUILD.json").exists():
        shutil.copy2(src / "files" / "llmwiki" / "BUILD.json", target / "llmwiki" / "BUILD.json")
    for rel in deletes:
        (target / rel).unlink(missing_ok=True)
    print(f"  wrote {len(writes)} files, deleted {len(deletes)}")
    write_manifest(target, meta["version"], keep=(installed or {}).get("files"), skip=set(skipped_root), local=set(local_kept))
    if not a.no_checks:
        _run(target, "manage.py", "migrate")
        _run(target, "manage.py", "doctor", "--offline", check=False)
    with open(bdir / "patch-log.txt", "a", encoding="utf-8") as f:
        f.write(f"{ts} applied {meta['version']} ({meta.get('note', '')}): {len(writes)} written, {len(deletes)} deleted, "
                f"conflicts forced: {conflicts if conflicts else 'none'}\n")
    if how == "task" and not a.no_restart:
        subprocess.run(["schtasks", "/Run", "/TN", "LLMWiki"], capture_output=True)
        print("  portal started again (scheduled task LLMWiki)")
    elif how == "process":
        print("  the portal was stopped: start it again with run.bat")
    print(f"Done: {target} now runs {meta['version']}.")


def write_manifest(target: Path, version: str | None = None, keep: dict | None = None, skip: set | None = None, local: set | None = None):
    """Record the code as installed now (rel → sha1). Files changed on this machine (`local`, or no longer in the code)
    keep their OLD hash, so the next patch still sees them as local changes."""
    files = {rel: sha1(p) for rel, p in code_files(target).items() if rel not in (skip or set())}
    for rel, h in (keep or {}).items():
        if (rel not in files or rel in (local or set())) and (target / rel).exists():
            files[rel] = h
    if version is None:
        bj = target / "llmwiki" / "BUILD.json"
        version = json.loads(bj.read_text(encoding="utf-8"))["version"] if bj.exists() else "dev-" + version_of(files).split("-")[-1]
    (target / MANIFEST).write_text(json.dumps({"version": version, "written": datetime.now().isoformat(timespec="seconds"), "files": files}, indent=1),
                                   encoding="utf-8")


def status(a):
    target = Path(a.target or ROOT).resolve()
    mf = target / MANIFEST
    if not mf.exists():
        print(f"{target}: no MANIFEST.json (run: python tools/patch.py manifest)")
        return
    m = json.loads(mf.read_text(encoding="utf-8"))
    changed = [rel for rel, h in m["files"].items() if (target / rel).exists() and sha1(target / rel) != h]
    gone = [rel for rel in m["files"] if not (target / rel).exists()]
    new = [rel for rel in code_files(target) if rel not in m["files"]]
    print(f"{target}: version {m['version']} (recorded {m.get('written', '?')})")
    print(f"  changed on this machine since: {len(changed)}" + "".join(f"\n    {r}" for r in changed[:40]))
    print(f"  new files: {len(new)}" + "".join(f"\n    {r}" for r in new[:20]))
    if gone:
        print(f"  deleted: {len(gone)}")


def rollback(a):
    target = Path(a.target or ROOT).resolve()
    bdir = target / "backups"
    zips = sorted(bdir.glob("code-before-*.zip"), key=lambda p: p.stat().st_mtime)
    if not zips:
        raise SystemExit("No code backup in backups\\")
    bz = Path(a.backup) if a.backup else zips[-1]
    how = "" if a.no_stop else stop_portal(target)
    with zipfile.ZipFile(bz) as z:
        info = json.loads(z.read("ROLLBACK.json"))
        for n in z.namelist():
            if n.startswith("files/") and not n.endswith("/"):
                dst = target / n[len("files/"):]
                dst.parent.mkdir(parents=True, exist_ok=True)
                with z.open(n) as s_, open(dst, "wb") as d_:
                    shutil.copyfileobj(s_, d_)
        for rel in info.get("added", []):
            (target / rel).unlink(missing_ok=True)
        if MANIFEST in z.namelist():
            (target / MANIFEST).write_bytes(z.read(MANIFEST))
    print(f"Code restored from {bz.name} (back to {info.get('previous') or 'the previous version'}).")
    print("If the patch had database migrations, also restore the database copy next to it: stop the portal, copy "
          f"backups\\db-before-{info['patch']}-*.sqlite3 to {_data_dir(target) / 'llmwiki.sqlite3'}.")
    if how == "task":
        subprocess.run(["schtasks", "/Run", "/TN", "LLMWiki"], capture_output=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="make a code patch zip from this checkout")
    b.add_argument("--out")
    b.add_argument("--note", default="")
    b.add_argument("--wheels", action="store_true", help="also pack the wheels of requirements.lock (needed when requirements changed)")
    b.add_argument("--platform", default="win_amd64")
    b.add_argument("--python", default="3.11")
    p = sub.add_parser("apply", help="apply an unzipped patch (or its zip) to an install")
    p.add_argument("src")
    p.add_argument("target_pos", nargs="?")
    p.add_argument("--target")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="overwrite files changed on this machine (they are in the backup)")
    p.add_argument("--no-restart", action="store_true")
    p.add_argument("--no-stop", action="store_true", help="do not stop the portal first (tests)")
    p.add_argument("--online", action="store_true", help="install changed requirements from PyPI / the company index")
    p.add_argument("--no-checks", action="store_true", help="skip migrate + doctor (tests only)")
    s = sub.add_parser("status")
    s.add_argument("--target")
    m = sub.add_parser("manifest")
    m.add_argument("--target")
    r = sub.add_parser("rollback")
    r.add_argument("--target")
    r.add_argument("--backup")
    r.add_argument("--no-stop", action="store_true")
    a = ap.parse_args()
    if a.cmd == "build":
        build(a)
    elif a.cmd == "apply":
        apply(a)
    elif a.cmd == "status":
        status(a)
    elif a.cmd == "manifest":
        write_manifest(Path(a.target or ROOT).resolve())
        print("MANIFEST.json written")
    elif a.cmd == "rollback":
        rollback(a)


if __name__ == "__main__":
    main()
