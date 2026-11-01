"""Optional cross-encoder reranker (P2, off by default; kept only if the eval shows a gain).
ONNX model (default Xenova/ms-marco-MiniLM-L-6-v2) in data/models/reranker."""
from __future__ import annotations

import logging
import os
import threading

import numpy as np

from llmwiki.conf import data_dir, get_settings

log = logging.getLogger(__name__)
_model = None
_lock = threading.Lock()


def _load():
    global _model
    with _lock:
        if _model is None:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            d = data_dir() / "models" / "reranker"
            if not (d / "onnx" / "model.onnx").exists():
                if os.environ.get("HF_HUB_OFFLINE") == "1":
                    raise FileNotFoundError(f"reranker files missing in {d}")
                from huggingface_hub import snapshot_download
                snapshot_download(get_settings().retrieval.rerank_model, allow_patterns=["onnx/model.onnx", "tokenizer.json", "config.json"], local_dir=str(d))
            tok = Tokenizer.from_file(str(d / "tokenizer.json"))
            tok.enable_truncation(max_length=512)
            tok.enable_padding()
            sess = ort.InferenceSession(str(d / "onnx" / "model.onnx"), providers=["CPUExecutionProvider"])
            _model = (tok, sess, {i.name for i in sess.get_inputs()})
    return _model


def scores(query: str, texts: list[str]) -> np.ndarray:
    tok, sess, names = _load()
    enc = tok.encode_batch([(query, t) for t in texts])
    feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64), "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
    if "token_type_ids" in names:
        feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
    out = sess.run(None, feed)[0]
    return out[:, 0] if out.ndim == 2 else out


def rerank(query: str, hits):
    s = scores(query, [h.chunk.header + "\n" + h.chunk.text for h in hits])
    for h, sc in zip(hits, s):
        h.extra["rerank"] = float(sc)
    return sorted(hits, key=lambda h: -h.extra["rerank"])
