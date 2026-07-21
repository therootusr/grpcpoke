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
        # PERF (future): recounts every line on each call -> O(n) per request,
        # O(n^2) over a run. Could seed an in-memory counter from this one-time
        # count at init/first-use, then increment it O(1) per record_request.
        # Keep the seed-from-file step: it's what lets ids resume correctly across
        # separate invocations on the same casebook. Assumes a single writer
        # (a concurrent external appender would make a cached counter drift).
        n = 0
        if os.path.exists(self.req_path):
            with open(self.req_path, "r") as f:
                n = sum(1 for _ in f)
        return f"{n:06d}"

    @staticmethod
    def _append(path: str, obj: dict) -> None:
        # PERF (future): the fsync below runs on every append -> durable, but the
        # per-request throughput bottleneck. A batched/group-commit path or an
        # opt-in no-fsync mode would raise throughput at the cost of crash-repro.
        with open(path, "a") as f:
            f.write(json.dumps(obj) + "\n")
            f.flush()
            os.fsync(f.fileno())
            # DURABILITY (future): fsyncs file *data* only. The parent-dir entry
            # isn't synced, so a crash right after a file is first created could
            # lose the link. Full durability needs a one-time fsync of the dir
            # after creation.

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
        # CONCURRENCY (future): single-writer only. Concurrent writers on one
        # casebook would collide on line-count ids and interleave appends; to
        # support them, serialize id+append under a file lock or namespace ids
        # per worker (e.g. a worker prefix).
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
        # PERF (future): O(n) linear scan. Fine as-is (called once per replay);
        # if replaying many ids in a loop, build a lazy {id: record} index.
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
