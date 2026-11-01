"""Embedding/reranker model files: `manage.py models --download [--all]` (uses HF_ENDPOINT, e.g. an internal mirror)."""
from django.core.management.base import BaseCommand

from llmwiki.conf import get_settings
from llmwiki.rag.embed import MODELS, download, model_dir


class Command(BaseCommand):
    help = "Download or list the local model files"

    def add_arguments(self, p):
        p.add_argument("--download", action="store_true")
        p.add_argument("--all", action="store_true", help="every eval model, not only the active one")

    def handle(self, *a, **o):
        s = get_settings().retrieval
        names = [m for m in (s.eval_models if o["all"] else [s.embed_model]) if m != "hashed"]
        for n in names:
            d = model_dir(n)
            have = (d / "onnx" / "model.onnx").exists()
            if o["download"] and not have:
                download(n)
                have = True
            self.stdout.write(f"{n}: {'present' if have else 'missing'} in {d} (repo {MODELS[n]['repo']})")
