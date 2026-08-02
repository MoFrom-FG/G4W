"""Template source for ``G4W-embedding/server.py`` (ST thin HTTP).

Installed by ``install_embedding.scaffold_layout``. Runs **only** inside the
addon venv (sentence-transformers + torch). G4W main process never
imports this module for inference — it talks HTTP only.
"""
from __future__ import annotations

from pathlib import Path

# Written verbatim to G4W-embedding/server.py (minus this module docstring
# wrapper). Keep stdlib + ST only.

ST_SERVER_PY = r'''#!/usr/bin/env python3
"""G4W embedding thin HTTP (Sentence-Transformers).

OpenAI-compatible:
  GET  /health
  POST /v1/embeddings   {"input": str|list[str], "model": optional}
  POST /embed           {"inputs": str|list[str]}   # TEI-ish alias

Env:
  EMBED_MODEL_DIR  default: <this_dir>/models/Qwen3-Embedding-0.6B
  EMBED_HOST       default: 127.0.0.1
  EMBED_PORT       default: 8081
  EMBED_DEVICE     default: auto (cuda when available, otherwise cpu)
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, List, Union
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
MODEL_DIR = Path(
    os.environ.get("EMBED_MODEL_DIR")
    or (ROOT / "models" / "Qwen3-Embedding-0.6B")
)
HOST = os.environ.get("EMBED_HOST", "127.0.0.1")
PORT = int(os.environ.get("EMBED_PORT") or os.environ.get("PORT") or "8081")
REQUESTED_DEVICE = os.environ.get("EMBED_DEVICE", "auto").strip().lower()
DEVICE = REQUESTED_DEVICE

_model = None
_model_err: str | None = None
_dim: int | None = None


def _load_model():
    global _model, _model_err, _dim, DEVICE
    if _model is not None:
        return _model
    if _model_err is not None:
        raise RuntimeError(_model_err)
    try:
        import torch
        from sentence_transformers import SentenceTransformer

        path = str(MODEL_DIR)
        if not MODEL_DIR.is_dir():
            raise FileNotFoundError(f"model dir missing: {path}")
        if REQUESTED_DEVICE == "auto":
            DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        elif REQUESTED_DEVICE.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError(
                "EMBED_DEVICE requests CUDA, but torch.cuda.is_available() is false"
            )
        else:
            DEVICE = REQUESTED_DEVICE
        _model = SentenceTransformer(path, device=DEVICE)
        # probe dim
        v = _model.encode(["ping"], normalize_embeddings=True)
        _dim = int(v.shape[-1])
        return _model
    except Exception as exc:
        _model_err = f"{type(exc).__name__}: {exc}"
        raise


def _as_list(inp: Any) -> List[str]:
    if inp is None:
        return []
    if isinstance(inp, str):
        return [inp]
    if isinstance(inp, list):
        return [str(x) for x in inp]
    return [str(inp)]


def _encode(texts: List[str]) -> List[List[float]]:
    model = _load_model()
    if not texts:
        return []
    vecs = model.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return [v.astype("float32").tolist() for v in vecs]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quieter
        sys.stderr.write("[embed-server] " + (fmt % args) + "\n")

    def _send(self, code: int, payload: dict, *, content_type: str = "application/json") -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/health", "/healthz"):
            try:
                _load_model()
                self._send(
                    200,
                    {
                        "ok": True,
                        "status": "ok",
                        "backend": "sentence-transformers",
                        "model_dir": str(MODEL_DIR),
                        "dim": _dim,
                        "device": DEVICE,
                    },
                )
            except Exception as exc:
                self._send(
                    503,
                    {
                        "ok": False,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            return
        if path in ("/", "/v1"):
            self._send(
                200,
                {
                    "service": "G4W-embedding",
                    "backend": "sentence-transformers",
                    "endpoints": ["/health", "/v1/embeddings", "/embed"],
                },
            )
            return
        self._send(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            data = self._read_json()
        except Exception as exc:
            self._send(400, {"error": f"invalid json: {exc}"})
            return
        try:
            if path in ("/v1/embeddings", "/embeddings"):
                texts = _as_list(data.get("input") if "input" in data else data.get("inputs"))
                model_name = data.get("model") or MODEL_DIR.name
                vectors = _encode(texts)
                self._send(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "object": "embedding",
                                "index": i,
                                "embedding": vec,
                            }
                            for i, vec in enumerate(vectors)
                        ],
                        "model": model_name,
                        "usage": {
                            "prompt_tokens": 0,
                            "total_tokens": 0,
                        },
                    },
                )
                return
            if path in ("/embed", "/tei"):
                texts = _as_list(data.get("inputs") if "inputs" in data else data.get("input"))
                vectors = _encode(texts)
                # TEI returns bare list of vectors for simple clients
                self._send(200, vectors)  # type: ignore[arg-type]
                return
            self._send(404, {"error": "not found", "path": path})
        except Exception as exc:
            traceback.print_exc()
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    print(
        f"[G4W-Embedding] ST server model={MODEL_DIR} http://{HOST}:{PORT}/",
        flush=True,
    )
    # Eager load so /health is honest and first request is not multi-minute
    try:
        _load_model()
        print(f"[G4W-Embedding] model ready dim={_dim} device={DEVICE}", flush=True)
    except Exception as exc:
        print(f"[G4W-Embedding] model load FAILED: {exc}", flush=True)
        # still bind — health returns 503 so lifecycle can report
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_server_py(dest: Path) -> Path:
    """Write server.py to embedding root."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(ST_SERVER_PY.lstrip("\n"), encoding="utf-8")
    return dest


__all__ = ["ST_SERVER_PY", "write_server_py"]
