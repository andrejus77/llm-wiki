"""Build an offline install zip for a server without internet (run on a machine WITH pip/HF access, e.g. the company laptop).

    python tools/package_offline.py [--platform win_amd64] [--python 3.11] [--models multilingual-e5-small,bge-small-en-v1.5] [--no-selftest]

Steps (each printed as it runs):
 1. pip download every pinned package in requirements.lock as wheels for the TARGET platform/Python
 2. model files: copied from data/models if present, else downloaded via HF_ENDPOINT (an internal mirror works)
 3. stage the code + the built frontend + install/run scripts (no data, no .venv, no company files)
 4. self-test: a fresh venv installs from the wheelhouse with --no-index and imports everything (when this machine's
    Python matches the target version)
 5. zip → dist/llmwiki-offline-<date>-<platform>-py<ver>.zip
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ["llmwiki", "llmwiki_site", "devtools", "tools", "docs", "frontend/dist", "manage.py", "requirements.txt", "requirements.lock",
           "README.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md", "eval/results"]
SKIP_DIRS = {"__pycache__", "node_modules", ".venv", ".git"}


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="win_amd64", help="win_amd64 (Windows Server) or manylinux2014_x86_64")
    ap.add_argument("--python", default="3.11")
    ap.add_argument("--models", default="multilingual-e5-small,bge-small-en-v1.5")
    ap.add_argument("--no-selftest", action="store_true")
    ap.add_argument("--with-data", nargs="?", const=str(ROOT / "data"), default=None, metavar="DATA_DIR",
                    help="also pack the processed data of this machine (default: .\\data) as import\\llmwiki-data.zip; "
                         "install.bat imports it on the server. The zip then holds company mail")
    a = ap.parse_args()
    pyv = a.python.replace(".", "")
    out = ROOT / "dist"
    stage = out / "stage" / "llmwiki"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    print("1/5 wheels for", a.platform, "Python", a.python, flush=True)
    wh = stage / "wheelhouse"
    req = ROOT / "requirements.lock"
    lines = [l for l in req.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    if not a.platform.startswith("win"):
        lines = [l for l in lines if not l.lower().startswith(("pywin32", "pyreadline3"))]
    tmp_req = stage / "requirements.lock"
    tmp_req.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--platform", a.platform, "--python-version", a.python,
         "--implementation", "cp", "--abi", f"cp{pyv}", "-r", str(tmp_req), "-d", str(wh)])

    print("2/5 model files", flush=True)
    for m in [x for x in a.models.split(",") if x]:
        src = ROOT / "data" / "models" / m
        dst = stage / "models" / m
        if (src / "onnx" / "model.onnx").exists():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".cache"))
            print(f"  copied {m}")
        else:
            from huggingface_hub import snapshot_download
            repo = {"multilingual-e5-small": "Xenova/multilingual-e5-small", "bge-small-en-v1.5": "Xenova/bge-small-en-v1.5"}[m]
            snapshot_download(repo_id=repo, allow_patterns=["onnx/model.onnx", "tokenizer.json", "config.json", "tokenizer_config.json", "special_tokens_map.json"],
                              local_dir=str(dst))
            print(f"  downloaded {m} via {os.environ.get('HF_ENDPOINT', 'huggingface.co')}")

    print("3/5 code + frontend + scripts", flush=True)
    for item in INCLUDE:
        s = ROOT / item
        if not s.exists():
            continue
        d = stage / item
        if s.is_dir():
            shutil.copytree(s, d, ignore=lambda dirpath, names: [n for n in names if n in SKIP_DIRS or n.endswith(".pyc")], dirs_exist_ok=True)
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
    for f in ("install.bat", "run.bat", "doctor.bat", "llmwiki.bat", "backfill.bat", "install_service.bat", "install.sh", "run.sh"):
        if (ROOT / "tools" / f).exists():
            shutil.copy2(ROOT / "tools" / f, stage / f)
    # version stamp (shown in the portal and by doctor; patches compare against MANIFEST.json, written by install.bat)
    sys.path.insert(0, str(ROOT / "tools"))
    from patch import code_files, sha1, version_of
    ver = version_of({k: sha1(p) for k, p in code_files(ROOT).items()})
    (stage / "llmwiki" / "BUILD.json").write_text(json.dumps({"version": ver, "built": date.today().isoformat(), "kind": "full package"}, indent=1),
                                                  encoding="utf-8")
    print(f"  version {ver}")

    if not a.no_selftest:
        print("4/5 self-test (fresh venv, --no-index)", flush=True)
        if f"{sys.version_info.major}.{sys.version_info.minor}" == a.python and (a.platform.startswith("win") == (os.name == "nt")):
            venv = Path(tempfile.mkdtemp(prefix="llmwiki-selftest-")) / "venv"
            run([sys.executable, "-m", "venv", str(venv)])
            py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            run([str(py), "-m", "pip", "install", "--no-index", "--find-links", str(wh), "-r", str(tmp_req), "-q"])
            run([str(py), "-c", "import django, qdrant_client, langgraph, tokenizers, openpyxl, pypdf, waitress; print('imports ok')"])
            run([str(py), "-c", "import onnxruntime; print('onnxruntime', onnxruntime.__version__)"])
            env = {**os.environ, "LLMWIKI_DATA": str(venv.parent / "data"), "HF_HUB_OFFLINE": "1"}
            run([str(py), str(stage / "manage.py"), "migrate", "-v", "0"], env=env, cwd=str(stage))
            run([str(py), str(stage / "manage.py"), "check"], env=env, cwd=str(stage))
            shutil.rmtree(venv.parent, ignore_errors=True)
        else:
            print(f"  skipped: this Python is {sys.version_info.major}.{sys.version_info.minor} on {os.name}, target is {a.python} on {a.platform}")

    if a.with_data:
        print("4b/5 data package from", a.with_data, flush=True)
        vpy = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not vpy.exists():
            raise SystemExit(f"--with-data needs the project venv ({vpy}); run setup.bat first")
        env = {**os.environ, "LLMWIKI_DATA": str(Path(a.with_data).resolve())}
        run([str(vpy), str(ROOT / "manage.py"), "export_data", str(stage / "import" / "llmwiki-data.zip")], env=env, cwd=str(ROOT))

    print("5/5 zip", flush=True)
    zname = out / f"llmwiki-offline-{date.today():%Y%m%d}-{a.platform}-py{pyv}{'-WITH-DATA' if a.with_data else ''}.zip"
    with zipfile.ZipFile(zname, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for p in stage.rglob("*"):
            if p.is_file():
                z.write(p, Path("llmwiki") / p.relative_to(stage))
    print(f"done: {zname} ({zname.stat().st_size / 1e6:.0f} MB). Copy it to the server, unzip, run install.bat."
          + (" It contains company mail: copy it only inside the company network." if a.with_data else ""))


if __name__ == "__main__":
    main()
