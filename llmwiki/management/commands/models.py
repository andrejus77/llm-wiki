"""Embedding/reranker model files: `manage.py models --download` gets every model in retrieval.eval_models (the active
one + the comparison model; both are needed for the eval and to switch models without a download), `--active` only the
one in use. Downloads go through retrieval.hf_endpoint (e.g. the company's Hugging Face mirror) if it is set."""
from django.core.management.base import BaseCommand

from llmwiki.conf import get_settings
from llmwiki.rag.embed import MODELS, download, model_dir


class Command(BaseCommand):
    help = "Download or list the local model files"

    def add_arguments(self, p):
        p.add_argument("--download", action="store_true")
        p.add_argument("--active", action="store_true", help="only the active embedding model")
        p.add_argument("--all", action="store_true", help="(default) every model in retrieval.eval_models")

    def handle(self, *a, **o):
        s = get_settings().retrieval
        names = [s.embed_model] if o["active"] else list(dict.fromkeys([s.embed_model] + s.eval_models))
        names = [m for m in names if m != "hashed"]
        for n in names:
            d = model_dir(n)
            have = (d / "onnx" / "model.onnx").exists()
            if o["download"] and not have:
                download(n)
                have = (d / "onnx" / "model.onnx").exists()
            self.stdout.write(f"{n}: {'present' if have else 'MISSING'} in {d} (repo {s.hf_repo_overrides.get(n, MODELS[n]['repo'])})"
                              + ("  ← in use" if n == s.embed_model else ""))
