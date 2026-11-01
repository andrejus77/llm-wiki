"""Code patches between machines: build → apply keeps data and local changes, detects conflicts, deletes removed files,
and can be rolled back."""
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "patch.py"


def run(*args, ok=True):
    r = subprocess.run([sys.executable, str(TOOL), *map(str, args)], capture_output=True, text=True, cwd=str(ROOT))
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    return r.stdout + r.stderr


def _patch(tmp: Path) -> Path:
    run("build", "--out", tmp / "dist", "--note", "test")
    z = next((tmp / "dist").glob("llmwiki-patch-*.zip"))
    with zipfile.ZipFile(z) as f:
        names = f.namelist()
        assert not any("/files/data" in n or n.endswith((".sqlite3", "company_adapter.py", "settings.yaml")) for n in names)   # code only
        f.extractall(tmp / "p")
    return next((tmp / "p").iterdir())


def test_patch_apply_keeps_data_and_local_changes(tmp_path):
    src = _patch(tmp_path)
    meta = json.loads((src / "PATCH.json").read_text(encoding="utf-8"))
    inst = tmp_path / "inst"
    # an "installed" machine: the same code, its own data, a manifest
    for rel in meta["files"]:
        d = inst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes((src / "files" / rel).read_bytes())
    (inst / "data").mkdir()
    (inst / "data" / "settings.yaml").write_text("server: {port: 8799}\n", encoding="utf-8")
    (inst / "data" / "company_adapter.py").write_text("# secret code\n", encoding="utf-8")
    run("manifest", "--target", inst)
    # this machine changes one file locally; the new code changes another, adds one and removes one
    (inst / "llmwiki" / "util.py").write_text((inst / "llmwiki" / "util.py").read_text(encoding="utf-8") + "\n# local fix\n", encoding="utf-8")
    conf = src / "files" / "llmwiki" / "conf.py"
    conf.write_text(conf.read_text(encoding="utf-8") + "\n# new in the patch\n", encoding="utf-8")
    new_file = src / "files" / "llmwiki" / "added_module.py"
    new_file.write_text("X = 1\n", encoding="utf-8")
    import hashlib
    meta["files"]["llmwiki/conf.py"] = hashlib.sha1(conf.read_bytes()).hexdigest()
    meta["files"]["llmwiki/added_module.py"] = hashlib.sha1(new_file.read_bytes()).hexdigest()
    del meta["files"]["docs/CONFIG.md"]
    (src / "PATCH.json").write_text(json.dumps(meta), encoding="utf-8")
    out = run("apply", src, "--target", inst, "--dry-run")
    assert "Dry run" in out and not (inst / "llmwiki" / "added_module.py").exists()
    run("apply", src, "--target", inst, "--no-stop", "--no-checks")
    assert "# new in the patch" in (inst / "llmwiki" / "conf.py").read_text(encoding="utf-8")
    assert (inst / "llmwiki" / "added_module.py").exists() and not (inst / "docs" / "CONFIG.md").exists()
    assert "# local fix" in (inst / "llmwiki" / "util.py").read_text(encoding="utf-8")          # the local change survives
    assert (inst / "data" / "company_adapter.py").read_text(encoding="utf-8") == "# secret code\n"  # data untouched
    m = json.loads((inst / "MANIFEST.json").read_text(encoding="utf-8"))
    assert m["version"] == meta["version"] and list((inst / "backups").glob("code-before-*.zip"))
    # a later patch that changes the locally changed file is a conflict unless --force
    util = src / "files" / "llmwiki" / "util.py"
    util.write_text(util.read_text(encoding="utf-8") + "\n# patch change\n", encoding="utf-8")
    meta["files"]["llmwiki/util.py"] = hashlib.sha1(util.read_bytes()).hexdigest()
    (src / "PATCH.json").write_text(json.dumps(meta), encoding="utf-8")
    assert "CONFLICT: llmwiki/util.py" in run("apply", src, "--target", inst, "--no-stop", "--no-checks", ok=False)
    # rollback restores the code before the first patch
    run("rollback", "--target", inst, "--no-stop")
    assert not (inst / "llmwiki" / "added_module.py").exists() and (inst / "docs" / "CONFIG.md").exists()
    assert "# new in the patch" not in (inst / "llmwiki" / "conf.py").read_text(encoding="utf-8")
