"""Test setup: a throw-away data folder, the mock LLM endpoint, hashed embeddings (no model files needed)."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="llmwiki-test-"))
os.environ["LLMWIKI_DATA"] = str(TMP)
os.environ["LLMWIKI_LOG_LEVEL"] = "WARNING"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "llmwiki_site.settings")
(TMP / "settings.yaml").write_text(
    "pool:\n  endpoints:\n    - {name: mock, kind: mock, rpm: 600, max_concurrent: 8}\n"
    "retrieval:\n  embed_model: hashed\n  vector_store: sqlite\n"
    "schedule:\n  chat_idle_resume_s: 0\n", encoding="utf-8")

import django  # noqa: E402

django.setup()
from django.core.management import call_command  # noqa: E402

call_command("migrate", verbosity=0)

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def data_dir():
    return TMP


def pytest_sessionfinish(session, exitstatus):
    try:
        from llmwiki.rag.vstore import QdrantStore
        QdrantStore.close()
        from django.db import connections
        connections.close_all()
        shutil.rmtree(TMP, ignore_errors=True)
    except Exception:
        pass
