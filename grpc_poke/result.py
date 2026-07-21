"""Structured result of a single gRPC probe call.

The point of this tool is OBSERVATION + deterministic REPLAY, so a call never
returns a bare byte string. It returns a CallResult recording exactly what
happened at the gRPC and connection layers — enough to (a) tell a normal
application rejection (e.g. INVALID_ARGUMENT) apart from a crash / reset / hang,
and (b) reproduce the request byte-for-byte later.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import List, Optional, Sequence, Tuple


class ConnState(str, Enum):
    """What we could infer about the connection from the outcome."""
    ALIVE_RESPONDED = "alive_responded"    # server received the RPC and returned a status (OK or app error)
    UNREACHABLE = "unreachable"            # never connected (refused / DNS / no route)
    HANDSHAKE_FAILED = "handshake_failed"  # TCP up but TLS/mTLS handshake or cert check failed
    CONNECTION_RESET = "connection_reset"  # dropped mid-RPC (RST_STREAM / socket closed / broken pipe)
    GOAWAY = "goaway"                      # server sent an HTTP/2 GOAWAY
    DEADLINE = "deadline"                  # client deadline hit (server slow / hung)
    UNKNOWN = "unknown"


# Status codes that mean "the server processed the call and produced a status" —
# i.e. the server is alive and answered. A normal INVALID_ARGUMENT lands here,
# which is exactly what lets us NOT confuse it with a crash.
_APP_ANSWER_CODES = {
    "OK", "CANCELLED", "INVALID_ARGUMENT", "NOT_FOUND", "ALREADY_EXISTS",
    "PERMISSION_DENIED", "UNAUTHENTICATED", "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION", "ABORTED", "OUT_OF_RANGE", "UNIMPLEMENTED",
    "DATA_LOSS",
}


def classify(code_name: str, blob: str) -> ConnState:
    """Best-effort connection state from the status code + error/debug text.

    `blob` should be the grpc-message concatenated with debug_error_string; the
    transport truth (RST_STREAM, GOAWAY, ssl handshake, connect failed) usually
    shows up there even when the status code is a generic UNAVAILABLE/INTERNAL.
    """
    b = (blob or "").lower()
    if code_name == "DEADLINE_EXCEEDED":
        return ConnState.DEADLINE
    # Transport keywords win regardless of the coarse status code.
    if "goaway" in b or "too_many_pings" in b:
        return ConnState.GOAWAY
    if any(k in b for k in ("handshake", "ssl", "tls", "certificate", "cert ",
                            "peer did not return a certificate", "no certificate",
                            "cert_verify", "handshake_failure", "key_usage")):
        return ConnState.HANDSHAKE_FAILED
    # Specific "connected, then dropped" signals must be checked BEFORE gRPC's
    # generic "failed to connect to all addresses" wrapper, which decorates almost
    # every transport error and would otherwise mask the real cause.
    if any(k in b for k in ("rst_stream", "reset by peer", "connection reset",
                            "socket closed", "transport closed", "broken pipe",
                            "closed the connection", "end of tcp stream",
                            "recv_message eof")):
        return ConnState.CONNECTION_RESET
    # Specific "never connected" signals.
    if any(k in b for k in ("connection refused", "conn refused", "no route to host",
                            "name resolution", "dns resolution", "unreachable")):
        return ConnState.UNREACHABLE
    if code_name in _APP_ANSWER_CODES:
        return ConnState.ALIVE_RESPONDED
    if code_name == "UNAVAILABLE":
        # Reached here only with the bare "failed to connect to all addresses"
        # wrapper and no specific signal → treat as never-established.
        return ConnState.UNREACHABLE
    if code_name in ("INTERNAL", "UNKNOWN"):
        # Server emitted a status with no transport hint → treat as answered
        # (these are interesting for a pentest: a server-side protocol/parse bug).
        return ConnState.ALIVE_RESPONDED
    return ConnState.UNKNOWN


MetaItem = Tuple[str, object]


def meta_to_jsonable(md: Optional[Sequence[MetaItem]]) -> List[list]:
    """gRPC metadata -> JSON-safe [key, value, is_binary]; -bin values b64'd."""
    out: List[list] = []
    for k, v in (md or []):
        if isinstance(v, (bytes, bytearray)):
            out.append([k, base64.b64encode(bytes(v)).decode("ascii"), True])
        else:
            out.append([k, v, False])
    return out


def meta_from_jsonable(items: Optional[Sequence[Sequence]]) -> List[MetaItem]:
    """Inverse of meta_to_jsonable, for replay."""
    md: List[MetaItem] = []
    for row in (items or []):
        k, v, is_bin = row[0], row[1], (row[2] if len(row) > 2 else False)
        md.append((k, base64.b64decode(v) if is_bin else v))
    return md


@dataclass
class CallResult:
    # identity / request (all persisted so a finding reproduces deterministically)
    request_id: str = "-"
    timestamp: float = 0.0
    target: str = ""
    transport: str = ""                 # "plaintext" | "mtls"
    method: str = ""
    request_len: int = 0
    request_b64: str = ""
    metadata_sent: list = field(default_factory=list)
    timeout_s: Optional[float] = None
    # outcome
    ok: bool = False
    status_code: str = "UNKNOWN"
    status_code_value: Optional[int] = None
    grpc_message: Optional[str] = None
    initial_metadata: list = field(default_factory=list)
    trailing_metadata: list = field(default_factory=list)
    response_len: int = 0
    response_b64: str = ""
    latency_ms: float = 0.0
    conn_state: str = ConnState.UNKNOWN.value
    debug_error_string: Optional[str] = None
    error_repr: Optional[str] = None

    @property
    def response_bytes(self) -> bytes:
        return base64.b64decode(self.response_b64) if self.response_b64 else b""

    @property
    def request_bytes(self) -> bytes:
        return base64.b64decode(self.request_b64) if self.request_b64 else b""

    def to_dict(self) -> dict:
        return asdict(self)
