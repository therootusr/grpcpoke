"""The core send primitive.

Probe.invoke() sends arbitrary bytes to a full gRPC method path and returns a
CallResult. It uses identity (pass-through) serializers, so it needs NO message
schema — grpcio still handles TLS/mTLS, HTTP/2 and gRPC length-prefix framing
correctly. Every outcome, success or failure, is captured (status, trailers,
response bytes, latency, connection state, debug string).
"""
from __future__ import annotations

import base64
import time
from typing import List, Optional, Sequence, Tuple

import grpc

from .result import CallResult, ConnState, classify, meta_to_jsonable


def _safe(fn):
    """Call `fn` (or return it if not callable), swallowing any exception."""
    try:
        return fn() if callable(fn) else fn
    except Exception:
        return None


def _code_name(code) -> str:
    return code.name if code is not None else "UNKNOWN"


def _code_value(code) -> Optional[int]:
    try:
        return int(code.value[0])
    except Exception:
        return None


class Probe:
    """Bound to one channel/target; issues raw unary calls."""

    def __init__(self, channel: grpc.Channel, target: str, transport: str):
        self._chan = channel
        self.target = target
        self.transport = transport

    def invoke(
        self,
        method: str,
        payload: bytes,
        *,
        metadata: Optional[Sequence[Tuple[str, object]]] = None,
        timeout: Optional[float] = None,
        request_id: str = "-",
        timestamp: Optional[float] = None,
    ) -> CallResult:
        if timestamp is None:
            timestamp = time.time()
        md: Optional[List[Tuple[str, object]]] = list(metadata) if metadata else None

        rpc = self._chan.unary_unary(
            method,
            request_serializer=lambda b: b,
            response_deserializer=lambda b: b,
        )

        base = dict(
            request_id=request_id,
            timestamp=timestamp,
            target=self.target,
            transport=self.transport,
            method=method,
            request_len=len(payload),
            request_b64=base64.b64encode(payload).decode("ascii"),
            metadata_sent=meta_to_jsonable(metadata),
            timeout_s=timeout,
        )

        t0 = time.perf_counter()
        try:
            resp, call = rpc.with_call(payload, metadata=md, timeout=timeout)
            latency = (time.perf_counter() - t0) * 1000.0
            return CallResult(
                **base,
                ok=True,
                status_code="OK",
                status_code_value=0,
                grpc_message="",
                initial_metadata=meta_to_jsonable(_safe(call.initial_metadata)),
                trailing_metadata=meta_to_jsonable(_safe(call.trailing_metadata)),
                response_len=len(resp),
                response_b64=base64.b64encode(resp).decode("ascii"),
                latency_ms=latency,
                conn_state=ConnState.ALIVE_RESPONDED.value,
                debug_error_string=_safe(call.debug_error_string),
                error_repr=None,
            )
        except grpc.RpcError as e:
            latency = (time.perf_counter() - t0) * 1000.0
            code = _safe(getattr(e, "code", None))
            code_name = _code_name(code)
            details = _safe(getattr(e, "details", None)) or ""
            debug = _safe(getattr(e, "debug_error_string", None))
            conn = classify(code_name, f"{details} {debug or ''}")
            return CallResult(
                **base,
                ok=False,
                status_code=code_name,
                status_code_value=_code_value(code),
                grpc_message=details,
                initial_metadata=meta_to_jsonable(_safe(getattr(e, "initial_metadata", None))),
                trailing_metadata=meta_to_jsonable(_safe(getattr(e, "trailing_metadata", None))),
                response_len=0,
                response_b64="",
                latency_ms=latency,
                conn_state=conn.value,
                debug_error_string=debug,
                error_repr=repr(e),
            )
        except Exception as e:  # non-gRPC failure (e.g. bad channel args)
            latency = (time.perf_counter() - t0) * 1000.0
            return CallResult(
                **base,
                ok=False,
                status_code="CLIENT_ERROR",
                status_code_value=None,
                grpc_message=str(e),
                latency_ms=latency,
                conn_state=ConnState.UNKNOWN.value,
                error_repr=repr(e),
            )
