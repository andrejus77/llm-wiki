"""Check a machine before (and after) installing: `manage.py doctor [--offline] [--ping]`.
Writes data/doctor-report.txt — send that file back when something doesn't work."""
import importlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings as dj
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Check Python, packages, the VC++ runtime, model files, POP3, the .pst, the LLM endpoints and the database"

    def add_arguments(self, p):
        p.add_argument("--offline", action="store_true", help="skip network checks (POP3, endpoints)")
        p.add_argument("--ping", action="store_true", help="send one tiny JSON request to every enabled endpoint")

    def handle(self, *a, **o):
        lines, bad = [], 0

        def check(name, ok, detail=""):
            nonlocal bad
            lines.append(f"[{'OK ' if ok else 'FAIL'}] {name}{(': ' + str(detail)) if detail else ''}")
            bad += 0 if ok else 1
            self.stdout.write(lines[-1])

        from llmwiki.conf import get_settings
        s = get_settings()
        from llmwiki.api import code_version
        check("code version", True, code_version())
        check("python", sys.version_info >= (3, 11), f"{platform.python_version()} on {platform.platform()}")
        check("sqlite FTS5 + trigram", self._fts(), sqlite3.sqlite_version)
        for mod in ["django", "ninja", "pydantic", "yaml", "waitress", "bs4", "openpyxl", "pypdf", "docx", "pptx", "numpy", "tokenizers", "huggingface_hub", "qdrant_client", "langgraph"]:
            try:
                m = importlib.import_module(mod)
                check(f"package {mod}", True, getattr(m, "__version__", ""))
            except Exception as e:
                check(f"package {mod}", False, e)
        # onnxruntime can crash the interpreter on import if the VC++ runtime is too old → test in a subprocess
        r = subprocess.run([sys.executable, "-c", "import onnxruntime;print(onnxruntime.__version__)"], capture_output=True, text=True, timeout=120)
        check("onnxruntime import (VC++ runtime)", r.returncode == 0, r.stdout.strip() or f"exit {r.returncode} — install the Microsoft VC++ 2015-2022 redistributable, or keep onnxruntime==1.18.1")
        if sys.platform == "win32":
            dll = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "msvcp140.dll"
            try:
                import win32api
                info = win32api.GetFileVersionInfo(str(dll), "\\")
                ver = f"{info['FileVersionMS'] >> 16}.{info['FileVersionMS'] & 0xFFFF}.{info['FileVersionLS'] >> 16}"
                check("msvcp140.dll version", True, ver)
            except Exception as e:
                check("msvcp140.dll version", dll.exists(), e)
        md = Path(dj.DATA_DIR) / "models"
        for name in ("multilingual-e5-small", "bge-small-en-v1.5"):
            f = md / name / "onnx" / "model.onnx"
            check(f"model files {name}", f.exists() or s.retrieval.embed_model != name, f if f.exists() else "missing (run: manage.py models --download)")
        try:
            from llmwiki.rag.embed import get_embedder
            t0 = time.time()
            e = get_embedder()
            v = e.embed(["delivery block Z1", "출하 보류"])
            check("embeddings", e.name == s.retrieval.embed_model, f"{e.name} dim {v.shape[1]} in {time.time() - t0:.1f}s")
        except Exception as ex:
            check("embeddings", False, ex)
        try:
            from llmwiki.models import Email
            check("database", True, f"{dj.DATABASES['default']['NAME']} · {Email.objects.count()} emails")
        except Exception as ex:
            check("database", False, f"{ex} (run: manage.py migrate)")
        if s.sources.pst_path:
            from llmwiki.sources import pst
            if s.sources.pst_path.strip().lower() in ("default", "outlook", "mailbox"):
                check(".pst: the Outlook profile's mailbox", pst.com_available(), "read through Outlook COM")
            else:
                check(".pst file", Path(s.sources.pst_path).exists(), s.sources.pst_path)
            check("Outlook COM (pywin32)", pst.com_available(), "classic Outlook needed for route 'com'")
            if s.sources.pst_route == "pypff" or (s.sources.pst_route == "auto" and not pst.com_available()):
                check("pypff", pst.pypff_available(), "needed for pst_route pypff")
            else:
                lines.append(f"[INFO] pypff (optional fallback for the .pst): {'installed' if pst.pypff_available() else 'not installed'}")
                self.stdout.write(lines[-1])
            lines.append(f"[INFO] .pst filter: {'only mail with ' + ', '.join(s.sources.dept_addresses) + ' in To/Cc' if s.sources.pst_dept_only else 'ALL mail (pst_dept_only is off)'}"
                         " — try `manage.py pst_scan --days 7` to count before importing")
        dps = [a.lower() for a in s.sources.dept_addresses]
        if s.departments.ai_address:
            check("departments: the AI address is one of the department addresses", s.departments.ai_address.lower() in dps, s.departments.ai_address)
        missing_names = [a for a in dps if a not in {k.lower() for k in s.departments.names}]
        lines.append(f"[INFO] departments: {len(dps)} reports ({', '.join(s.departments.names.get(a, a.split('@')[0]) for a in s.sources.dept_addresses)})"
                     + (f"; no display name for {', '.join(missing_names)}" if missing_names else ""))
        lines.append(f"[INFO] team members: {len(s.departments.team_members)} listed" if s.departments.team_members else
                     "[WARN] departments.team_members is empty: every internal sender counts as a team member for the reports")
        ext = [ep for ep in s.pool.endpoints if ep.enabled and ep.external]
        if ext:
            lines.append(f"[INFO] privacy: external endpoints {', '.join(e.name for e in ext)} may serve "
                         f"{'EVERY stage (test_data_only — synthetic data only!)' if s.privacy.test_data_only else ', '.join(s.privacy.external_stages)}; PII removed from their prompts")
        if s.privacy.test_data_only:
            from llmwiki.models import Email
            real = Email.objects.exclude(from_addr__endswith=".example").count()
            check("privacy: test_data_only only with synthetic mail", real == 0, f"{real} emails from non-.example senders")
        if s.sources.pop3_enabled:
            from llmwiki.sources.pop3 import accounts, connect
            accs = accounts(s.sources)
            check("POP3 mailboxes configured", bool(accs), f"{len(accs)} mailbox(es)" if accs else "pop3_enabled but no pop3_accounts")
            depts = [d.lower() for d in s.sources.dept_addresses]
            for a in accs:
                name = f"POP3 {a['user']}@{a['host']}"
                if a["dept"] and a["dept"].lower() not in depts:
                    check(f"{name}: department", False, f"{a['dept']} is not in sources.dept_addresses")
                if not os.environ.get(a["password_env"]):
                    check(f"{name}: password", False, f"environment variable {a['password_env']} is not set")
                    continue
                if o["offline"]:
                    continue
                try:
                    c = connect(a, timeout=20)
                    n, size = c.stat()
                    c.quit()
                    check(f"{name}: login", True, f"{n} messages on the server ({size / 1e6:.0f} MB)")
                except Exception as ex:
                    check(f"{name}: login", False, ex)
        for ep in s.pool.endpoints:
            if not ep.enabled:
                continue
            detail = f"{ep.kind} rpm {ep.rpm} · {ep.max_concurrent} parallel · timeout {ep.timeout_s or 'none'}"
            if ep.kind == "custom":
                p = Path(os.environ.get("LLMWIKI_ADAPTER", Path(dj.DATA_DIR) / "company_adapter.py"))
                check(f"endpoint {ep.name}: company_adapter.py", p.exists(), p)
            if ep.kind in ("claude_cli", "gemini_cli", "codex_cli"):
                import shutil
                exe = {"claude_cli": "claude", "gemini_cli": "gemini", "codex_cli": "codex"}[ep.kind]
                check(f"endpoint {ep.name}: {exe} CLI installed", bool(shutil.which(exe)), shutil.which(exe) or f"npm i -g … / see docs\\OPERATIONS.md; then log in once with `{exe}`")
            if ep.api_key_env:
                check(f"endpoint {ep.name}: env {ep.api_key_env}", bool(os.environ.get(ep.api_key_env)), "set" if os.environ.get(ep.api_key_env) else "not set")
            if o["ping"] and not o["offline"]:
                try:
                    from llmwiki.llm.adapters import make_adapter
                    from llmwiki.llm.jsonparse import extract_json
                    t0 = time.time()
                    txt = make_adapter(ep, s.pool.endpoints).complete("Reply with JSON only.", 'Return exactly: {"ok": true, "n": 3}', ep.timeout_s or 120, 50)
                    obj, err = extract_json(txt)
                    check(f"endpoint {ep.name}: ping", bool(obj and obj.get("ok")), f"{time.time() - t0:.1f}s · {txt[:80]!r}")
                except Exception as ex:
                    check(f"endpoint {ep.name}: ping", False, ex)
            else:
                lines.append(f"[INFO] endpoint {ep.name}: {detail}")
        rep = Path(dj.DATA_DIR) / "doctor-report.txt"
        rep.write_text("\n".join(lines) + f"\n\nsettings (no secrets):\n{json.dumps(json.loads(s.model_dump_json()), indent=1)[:6000]}\n", encoding="utf-8")
        self.stdout.write(f"\n{bad} problem(s). Report: {rep}")

    @staticmethod
    def _fts():
        try:
            c = sqlite3.connect(":memory:")
            c.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
            return True
        except Exception:
            return False
