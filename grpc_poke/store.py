"""Casebook: an append-only, crash-safe record of every request + result.

The request record is flushed AND fsync'd to disk BEFORE the call is issued, so
a payload that hangs or crashes the server is already durably recorded and can
be replayed deterministically. Results are appended after the call returns.

Layout under <root>/:
  requests.jsonl  one line per send  (id, ts, target, transport, method,
                                       payload_b64, metadata, timeout, [replay_of])
  results.jsonl   one line per send  (full CallResult.to_dict())
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Iterator, Optional, Sequence, Tuple

from .result import CallResult, meta_to_jsonable


class Casebook:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.req_path = os.path.join(root, "requests.jsonl")
        self.res_path = os.path.join(root, "results.jsonl")

    def _next_id(self) -> str:
        n = 0
        if os.path.exists(self.req_path):
            with open(self.req_path, "r") as f:
                n = sum(1 for _ in f)
        return f"{n:06d}"

    @staticmethod
    def _append(path: str, obj: dict) -> None:
        with open(path, "a") as f:
            f.write(json.dumps(obj) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def record_request(
        self,
        *,
        target: str,
        transport: str,
        method: str,
        payload: bytes,
        metadata: Optional[Sequence[Tuple[str, object]]] = None,
        timeout: Optional[float] = None,
        replay_of: Optional[str] = None,
    ) -> str:
        rid = self._next_id()
        rec = {
            "id": rid,
            "ts": time.time(),
            "target": target,
            "transport": transport,
            "method": method,
            "payload_b64": base64.b64encode(payload).decode("ascii"),
            "payload_len": len(payload),
            "metadata": meta_to_jsonable(metadata),
            "timeout_s": timeout,
        }
        if replay_of is not None:
            rec["replay_of"] = replay_of
        self._append(self.req_path, rec)
        return rid

    def record_result(self, result: CallResult) -> None:
        self._append(self.res_path, result.to_dict())

    def get_request(self, rid: str) -> dict:
        if os.path.exists(self.req_path):
            with open(self.req_path, "r") as f:
                for line in f:
                    rec = json.loads(line)
                    if rec.get("id") == rid:
                        return rec
        raise KeyError(f"no request with id {rid!r} in {self.req_path}")

    def iter_requests(self) -> Iterator[dict]:
        if not os.path.exists(self.req_path):
            return
        with open(self.req_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)
