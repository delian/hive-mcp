"""Local ONNX embedding (bge-small-en-v1.5, int8): CLS pooling, L2-normalised, 384-dim."""

from __future__ import annotations

import time

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from bench.common import DATA_ROOT

REAL = DATA_ROOT / "real"


class Embedder:
    def __init__(self, max_len: int = 128, threads: int = 4) -> None:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(str(REAL / "model_q.onnx"), opts, providers=["CPUExecutionProvider"])
        self.tok = Tokenizer.from_file(str(REAL / "tokenizer.json"))
        self.tok.enable_truncation(max_length=max_len)
        self.tok.enable_padding(length=None)
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def encode(self, texts: list[str]) -> np.ndarray:
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        out = self.sess.run(None, feed)[0]
        cls = out[:, 0, :]
        return (cls / np.linalg.norm(cls, axis=1, keepdims=True)).astype(np.float32)


if __name__ == "__main__":
    e = Embedder()
    a = e.encode(["The radar measures range using pulse delay.", "Radar range is computed from echo time delay.",
                  "A recipe for chocolate cake with butter."])
    print(a.shape, "sim(radar,radar)=%.3f sim(radar,cake)=%.3f" % (a[0] @ a[1], a[0] @ a[2]))
    txt = ["word " * 110] * 64
    t = time.perf_counter(); e.encode(txt); dt = time.perf_counter() - t
    print("throughput: %.0f chunks/s at ~128 tokens" % (64 / dt))
