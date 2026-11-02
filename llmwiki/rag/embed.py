"""Local embeddings: ONNX models run with onnxruntime + tokenizers (no PyTorch, nothing leaves the machine).

Models live in data/models/<name>/ (downloaded once through the HF mirror by `manage.py models --download`, or
copied there by package.bat). With HF_HUB_OFFLINE=1 nothing is downloaded at runtime.

- multilingual-e5-small: mean pooling, "query: " / "passage: " prefixes. Handles Korean. Work default.
- bge-small-en-v1.5: CLS pooling, query instruction prefix. English baseline.
- hashed: no model at all — word/char n-gram hashing. The degraded fallback.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import zlib
from pathlib import Path

import numpy as np

from llmwiki.conf import data_dir, get_settings

log = logging.getLogger(__name__)

MODELS = {
    "multilingual-e5-small": {"repo": "Xenova/multilingual-e5-small", "pool": "mean", "q": "query: ", "d": "passage: ", "dim": 384},
    "bge-small-en-v1.5": {"repo": "Xenova/bge-small-en-v1.5", "pool": "cls", "q": "Represent this sentence for searching relevant passages: ", "d": "", "dim": 384},
    "hashed": {"dim": 768},
}
FILES = ["onnx/model.onnx", "tokenizer.json", "config.json", "tokenizer_config.json", "special_tokens_map.json"]


def model_dir(name: str) -> Path:
    return data_dir() / "models" / name


def download(name: str) -> Path:
    mirror = get_settings().retrieval.hf_endpoint
    if mirror:   # huggingface_hub reads its endpoint from the environment when it is imported
        os.environ["HF_ENDPOINT"] = mirror.rstrip("/")
        os.environ.pop("HF_HUB_OFFLINE", None)
    from huggingface_hub import snapshot_download
    info = MODELS[name]
    repo = get_settings().retrieval.hf_repo_overrides.get(name, info["repo"])
    d = model_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=repo, allow_patterns=FILES, local_dir=str(d))
    log.info("downloaded %s from %s (HF_ENDPOINT=%s) to %s", name, repo, os.environ.get("HF_ENDPOINT", "default"), d)
    return d


class HashedEmbedder:
    name = "hashed"
    dim = MODELS["hashed"]["dim"]

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        t = (text or "").lower()
        toks = re.findall(r"[\w가-힣]+", t)
        feats = toks + [a + " " + b for a, b in zip(toks, toks[1:])]
        for w in toks:  # char trigrams help with inflection and Hangul
            feats += [w[i:i + 3] for i in range(max(1, len(w) - 2))]
        for f in feats:
            h = zlib.crc32(f.encode("utf-8"))
            v[h % self.dim] += 1.0 if (h >> 16) & 1 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed(self, texts: list[str], query: bool = False) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim), dtype=np.float32)


class OnnxEmbedder:
    def __init__(self, name: str):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.name = name
        self.info = MODELS[name]
        d = model_dir(name)
        if not (d / "onnx" / "model.onnx").exists():
            if os.environ.get("HF_HUB_OFFLINE") == "1":
                raise FileNotFoundError(f"model files missing in {d} and HF_HUB_OFFLINE=1")
            download(name)
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.enable_truncation(max_length=512)
        self.tok.enable_padding(pad_id=self.tok.token_to_id("<pad>") if self.tok.token_to_id("<pad>") is not None else 0)
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)
        self.sess = ort.InferenceSession(str(d / "onnx" / "model.onnx"), sess_options=so, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.dim = self.info["dim"]
        self.lock = threading.Lock()

    def embed(self, texts: list[str], query: bool = False) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        pre = self.info["q"] if query else self.info["d"]
        out = []
        bs = get_settings().retrieval.embed_batch
        for i in range(0, len(texts), bs):
            batch = [pre + (t or " ") for t in texts[i:i + bs]]
            enc = self.tok.encode_batch(batch)
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self.sess.run(None, feed)[0]
            if self.info["pool"] == "cls":
                v = hidden[:, 0]
            else:
                m = mask[..., None].astype(np.float32)
                v = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
            v = v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-9, None)
            out.append(v.astype(np.float32))
        return np.concatenate(out)


_cache: dict[str, object] = {}
_lock = threading.Lock()


def get_embedder(name: str | None = None):
    """The active (or named) embedder. Falls back to hashed vectors if the model can't be loaded (logged once)."""
    name = name or get_settings().retrieval.embed_model
    with _lock:
        if name not in _cache:
            if name == "hashed":
                _cache[name] = HashedEmbedder()
            else:
                try:
                    _cache[name] = OnnxEmbedder(name)
                except Exception as e:
                    log.error("embedding model %s unavailable (%s) — using hashed vectors (degraded)", name, e)
                    from llmwiki.logs import event
                    event("index", f"Embedding model {name} could not be loaded; using hashed vectors (degraded): {e}", level="error")
                    _cache[name] = HashedEmbedder()
        return _cache[name]


def to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype="<f4").tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(bytes(b), dtype="<f4")
