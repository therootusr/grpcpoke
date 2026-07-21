"""Command-line interface: `python -m grpc_poke ...`

Subcommands:
  call         send one request (raw bytes, or --json with a --protoset) and
               print+persist a structured result.
  replay       re-send a previously recorded request by id (deterministic repro).
  decode       schema-free structural decode of protobuf bytes (or typed with
               a --protoset).
  list-methods print method paths from a --protoset or a --methods file.
  selftest     run the offline end-to-end self-test (no server needed).
"""
from __future__ import annotations

import argparse
import base64
import json
from typing import List, Optional, Tuple

from . import methods as M
from .channel import build_channel
from .client import Probe
from .codec import Schema, decode_raw
from .result import meta_from_jsonable
from .store import Casebook

DEFAULT_CASEBOOK = "./casebook"


# --------------------------------------------------------------------------- #
# argument helpers
# --------------------------------------------------------------------------- #

def _add_transport_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("target", help="host:port (e.g. 127.0.0.1:PORT)")
    g = p.add_argument_group("transport")
    g.add_argument("--plaintext", action="store_true",
                   help="insecure/plaintext channel (no TLS)")
    g.add_argument("--ca", metavar="PEM", help="root CA to verify the server (mTLS)")
    g.add_argument("--cert", metavar="PEM", help="client cert chain (mTLS)")
    g.add_argument("--key", metavar="PEM", help="client private key (mTLS)")
    g.add_argument("--server-name", default=None,
                   help="TLS target-name/authority override (set when the server "
                        "cert CN/SAN differs from the connect address)")
    g.add_argument("--max-msg", type=int, default=64 * 1024 * 1024,
                   help="max send/recv message bytes (default 64MiB)")


def _add_method_args(p: argparse.ArgumentParser) -> None:
    mg = p.add_argument_group("method")
    mg.add_argument("--method", help="full path, e.g. /pkg.Service/Method")
    mg.add_argument("--rpc", help="short RPC name (resolved via --service/--methods/--protoset)")
    mg.add_argument("--service", help="fully-qualified service (e.g. pkg.Service) to prefix --rpc/--method")
    mg.add_argument("--methods", metavar="FILE",
                    help="file of method paths (one per line) to resolve --rpc / feed list-methods")


def _channel_and_transport(a) -> Tuple[object, str]:
    if a.plaintext:
        return (build_channel(a.target, plaintext=True, max_msg=a.max_msg), "plaintext")
    return (build_channel(a.target, plaintext=False, ca=a.ca, cert=a.cert, key=a.key,
                          server_name=a.server_name, max_msg=a.max_msg), "mtls")


def _methods_list(a, schema: Optional[Schema]) -> Optional[List[str]]:
    out: List[str] = []
    if getattr(a, "methods", None):
        out += M.read_methods_file(a.methods)
    if schema:
        out += schema.list_methods()
    return out or None


def _resolve_method(a, schema: Optional[Schema]) -> str:
    service = getattr(a, "service", None)
    if getattr(a, "method", None):
        return M.full_path(a.method, service=service)
    if getattr(a, "rpc", None):
        try:
            return M.resolve_rpc(a.rpc, methods=_methods_list(a, schema), service=service)
        except ValueError as e:
            raise SystemExit(f"error: {e}")
    raise SystemExit("error: one of --method or --rpc is required")


def _resolve_payload(a, schema: Optional[Schema], method: str) -> bytes:
    chosen: List[Tuple[str, object]] = [
        (n, v) for n, v in (
            ("empty", a.empty),
            ("data-file", a.data_file),
            ("data-hex", a.data_hex),
            ("data-b64", a.data_b64),
            ("json", a.json),
        ) if v
    ]
    if len(chosen) != 1:
        raise SystemExit("error: give exactly one of "
                         "--empty/--data-file/--data-hex/--data-b64/--json")
    name, val = chosen[0]
    if name == "empty":
        return b""
    if name == "data-file":
        with open(val, "rb") as f:
            return f.read()
    if name == "data-hex":
        return bytes.fromhex(str(val).strip().replace(" ", "").replace("\n", ""))
    if name == "data-b64":
        return base64.b64decode(val)
    if name == "json":
        if not schema:
            raise SystemExit("error: --json requires --protoset")
        return schema.encode_json(method, val)
    raise SystemExit("error: no payload")  # unreachable


def _parse_metadata(items) -> List[Tuple[str, object]]:
    md: List[Tuple[str, object]] = []
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"error: bad --metadata {it!r} (want key=value)")
        k, v = it.split("=", 1)
        k = k.lower()
        md.append((k, base64.b64decode(v) if k.endswith("-bin") else v))
    return md


def _load_schema(path: Optional[str]) -> Optional[Schema]:
    if not path:
        return None
    with open(path, "rb") as f:
        return Schema(f.read())


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line for line in text.splitlines())


def _fmt_meta(md) -> list:
    return [f"{k}={v}" for k, v, _bin in md]


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def _print_result(res, schema=None, method=None, as_json=False) -> None:
    if as_json:
        print(json.dumps(res.to_dict(), indent=2))
        return
    tag = "OK " if res.ok else "ERR"
    print(f"[{tag}] {res.method}")
    print(f"      id={res.request_id}  target={res.target} ({res.transport})  "
          f"latency={res.latency_ms:.1f}ms")
    print(f"      status={res.status_code}"
          f"{'' if res.status_code_value is None else f'({res.status_code_value})'}"
          f"  conn_state={res.conn_state}")
    if res.grpc_message:
        print(f"      grpc-message: {res.grpc_message}")
    if not res.ok and res.debug_error_string:
        print(f"      debug: {res.debug_error_string}")
    if res.trailing_metadata:
        print(f"      trailers: {_fmt_meta(res.trailing_metadata)}")
    print(f"      request={res.request_len}B  response={res.response_len}B")
    if res.response_len:
        try:
            print("      response (raw-decode):")
            print(_indent(json.dumps(decode_raw(res.response_bytes), indent=2), 8))
        except Exception as e:
            print(f"        <raw-decode failed: {e}>  hex={res.response_bytes.hex()}")
        if schema and method:
            try:
                print("      response (typed):")
                print(_indent(schema.decode_response(method, res.response_bytes), 8))
            except Exception as e:
                print(f"        <typed decode failed: {e}>")


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_call(a) -> int:
    schema = _load_schema(a.protoset)
    method = _resolve_method(a, schema)
    payload = _resolve_payload(a, schema, method)
    metadata = _parse_metadata(a.metadata)
    chan, transport = _channel_and_transport(a)
    try:
        cb = None if a.no_store else Casebook(a.casebook)
        rid = "-"
        if cb:
            rid = cb.record_request(target=a.target, transport=transport, method=method,
                                    payload=payload, metadata=metadata, timeout=a.timeout)
        res = Probe(chan, a.target, transport).invoke(
            method, payload, metadata=metadata, timeout=a.timeout, request_id=rid)
        if cb:
            cb.record_result(res)
        _print_result(res, schema=schema, method=method, as_json=a.out_json)
        if cb and not a.out_json:
            print(f"      recorded: {cb.req_path} (id={rid})")
        return 0 if res.ok else 2
    finally:
        chan.close()


def cmd_replay(a) -> int:
    cb = Casebook(a.casebook)
    rec = cb.get_request(a.id)
    method = rec["method"]
    payload = base64.b64decode(rec["payload_b64"])
    metadata = meta_from_jsonable(rec.get("metadata"))
    timeout = rec.get("timeout_s")

    override = a.target or a.plaintext or a.ca or a.cert or a.key
    if override:
        target = a.target or rec["target"]
        chan, transport = _channel_and_transport(
            argparse.Namespace(target=target, plaintext=a.plaintext, ca=a.ca, cert=a.cert,
                               key=a.key, server_name=a.server_name, max_msg=a.max_msg))
    else:
        target = rec["target"]
        transport = rec["transport"]
        if transport == "mtls":
            raise SystemExit(
                "error: stored request used mTLS; key material is not persisted. "
                "Re-supply --ca/--cert/--key (and --target if different) to replay it.")
        chan, transport = (build_channel(target, plaintext=True, max_msg=a.max_msg), "plaintext")

    try:
        new_id = cb.record_request(target=target, transport=transport, method=method,
                                   payload=payload, metadata=metadata, timeout=timeout,
                                   replay_of=a.id)
        res = Probe(chan, target, transport).invoke(
            method, payload, metadata=metadata, timeout=timeout, request_id=new_id)
        cb.record_result(res)
        print(f"(replay of {a.id} -> new id {new_id})")
        _print_result(res, as_json=a.out_json)
        return 0 if res.ok else 2
    finally:
        chan.close()


def cmd_decode(a) -> int:
    if a.data_file:
        with open(a.data_file, "rb") as f:
            data = f.read()
    elif a.data_hex:
        data = bytes.fromhex(a.data_hex.strip().replace(" ", "").replace("\n", ""))
    elif a.data_b64:
        data = base64.b64decode(a.data_b64)
    else:
        raise SystemExit("error: give one of --data-file/--data-hex/--data-b64")

    if a.protoset and (a.as_type or a.as_response):
        schema = _load_schema(a.protoset)
        if a.as_type:
            print(schema.decode_message(a.as_type, data))
        else:
            print(schema.decode_response(schema.find_method(a.as_response), data))
    else:
        print(json.dumps(decode_raw(data), indent=2))
    return 0


def cmd_list_methods(a) -> int:
    if a.protoset:
        for m in _load_schema(a.protoset).list_methods():
            print(m)
    elif a.methods:
        for m in M.read_methods_file(a.methods):
            print(m)
    else:
        raise SystemExit("error: list-methods needs --protoset or --methods FILE")
    return 0


def cmd_selftest(a) -> int:
    # Load the sibling selftest.py (it sits next to the package dir) regardless
    # of cwd, and run it. Works both in-repo and in the delivered bundle.
    import importlib.util
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "grpcpoke_selftest", os.path.join(here, "selftest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main()


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m grpc_poke",
        description="mTLS/plaintext gRPC send-observe-replay harness.")
    sub = p.add_subparsers(dest="cmd", required=True)

    # call
    c = sub.add_parser("call", help="send one request and record the result")
    _add_transport_args(c)
    _add_method_args(c)
    pg = c.add_argument_group("payload (choose one)")
    pg.add_argument("--empty", action="store_true", help="empty message body")
    pg.add_argument("--data-file", help="raw request bytes from file")
    pg.add_argument("--data-hex", help="raw request bytes as hex")
    pg.add_argument("--data-b64", help="raw request bytes as base64")
    pg.add_argument("--json", help="well-formed request as JSON (requires --protoset)")
    c.add_argument("--metadata", action="append", default=[], metavar="K=V",
                   help="gRPC metadata (repeatable; keys ending -bin take base64 values)")
    c.add_argument("--timeout", type=float, default=None, help="deadline in seconds")
    c.add_argument("--protoset", help="FileDescriptorSet for --json / typed decode / --rpc resolution")
    c.add_argument("--casebook", default=DEFAULT_CASEBOOK, help="record dir (default %(default)s)")
    c.add_argument("--no-store", action="store_true", help="do not persist the request/result")
    c.add_argument("--out-json", action="store_true", help="print the result as JSON")
    c.set_defaults(func=cmd_call)

    # replay
    r = sub.add_parser("replay", help="re-send a recorded request by id")
    r.add_argument("id", help="request id from the casebook (e.g. 000003)")
    r.add_argument("--casebook", default=DEFAULT_CASEBOOK, help="record dir (default %(default)s)")
    r.add_argument("--target", help="override target host:port")
    r.add_argument("--plaintext", action="store_true", help="force plaintext transport")
    r.add_argument("--ca", help="root CA (mTLS replay)")
    r.add_argument("--cert", help="client cert (mTLS replay)")
    r.add_argument("--key", help="client key (mTLS replay)")
    r.add_argument("--server-name", default=None, help="TLS name override")
    r.add_argument("--max-msg", type=int, default=64 * 1024 * 1024)
    r.add_argument("--out-json", action="store_true")
    r.set_defaults(func=cmd_replay)

    # decode
    d = sub.add_parser("decode", help="decode protobuf bytes (schema-free or typed)")
    d.add_argument("--data-file")
    d.add_argument("--data-hex")
    d.add_argument("--data-b64")
    d.add_argument("--protoset", help="FileDescriptorSet for typed decode")
    d.add_argument("--as-type", help="typed decode as this message full-name (with --protoset)")
    d.add_argument("--as-response", help="typed decode as this RPC's response (with --protoset)")
    d.set_defaults(func=cmd_decode)

    # list-methods
    lm = sub.add_parser("list-methods", help="print method paths from --protoset or --methods FILE")
    lm.add_argument("--protoset", help="FileDescriptorSet to enumerate methods from")
    lm.add_argument("--methods", metavar="FILE", help="file of method paths to print")
    lm.set_defaults(func=cmd_list_methods)

    # selftest
    st = sub.add_parser("selftest", help="run the offline end-to-end self-test (no server needed)")
    st.set_defaults(func=cmd_selftest)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except OSError as e:
        # a missing/unreadable file (--methods, --protoset, --data-file, --ca/--cert/--key)
        fn = getattr(e, "filename", None)
        raise SystemExit(
            f"error: cannot open {fn!r}: {e.strerror}" if fn and e.strerror
            else f"error: {e}")
