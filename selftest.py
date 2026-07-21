"""Offline self-test: stands up a local generic gRPC server and drives the probe
end-to-end. No external server, schema, or network required. Run:

    python selftest.py            # or: ./grpcpoke selftest

Exercises: OK path + response decode, application-error classification
(INVALID_ARGUMENT must read as alive_responded, NOT a crash), unimplemented and
unreachable classification, casebook record round-trip, and the typed
(--protoset) path using an in-memory descriptor set built here (no protoc).
"""
import sys
import tempfile
from concurrent import futures

import grpc

from grpc_poke import Probe, Schema, build_channel, decode_raw
from grpc_poke.store import Casebook

SVC = "probe.selftest.v1.Echo"
SERVER_N = 1720000000000000


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


# PingResult { n = 1 } -> tag 0x08 (field 1, varint) + value
PING_REPLY = b"\x08" + _varint(SERVER_N)


class Handler(grpc.GenericRpcHandler):
    def service(self, details):
        m = details.method
        if m.endswith("/Ping"):
            return grpc.unary_unary_rpc_method_handler(self._ping)
        if m.endswith("/Boom"):
            return grpc.unary_unary_rpc_method_handler(self._boom)
        return None  # -> UNIMPLEMENTED

    def _ping(self, request, context):
        return PING_REPLY  # request/response are raw bytes (no serializers)

    def _boom(self, request, context):
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bad request")


def _selftest_protoset():
    """Build a minimal FileDescriptorSet in memory (no protoc needed)."""
    from google.protobuf import descriptor_pb2 as dpb
    fdp = dpb.FileDescriptorProto()
    fdp.name = "probe_selftest.proto"
    fdp.package = "probe.selftest.v1"
    fdp.syntax = "proto3"
    for mname in ("PingArg", "PingResult"):
        mt = fdp.message_type.add()
        mt.name = mname
        fld = mt.field.add()
        fld.name = "n"
        fld.number = 1
        fld.label = dpb.FieldDescriptorProto.LABEL_OPTIONAL
        fld.type = dpb.FieldDescriptorProto.TYPE_INT64
    svc = fdp.service.add()
    svc.name = "Echo"
    for mname in ("Ping", "Boom"):
        m = svc.method.add()
        m.name = mname
        m.input_type = ".probe.selftest.v1.PingArg"
        m.output_type = ".probe.selftest.v1.PingResult"
    fds = dpb.FileDescriptorSet()
    fds.file.append(fdp)
    return fds.SerializeToString()


def main():
    ok = True

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    server.add_generic_rpc_handlers([Handler()])
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    target = f"127.0.0.1:{port}"
    print(f"local test server on {target}")

    try:
        chan = build_channel(target, plaintext=True)
        probe = Probe(chan, target, "plaintext")

        # 1) success + response decode
        r = probe.invoke(f"/{SVC}/Ping", b"")
        assert r.ok and r.status_code == "OK", r.to_dict()
        assert r.conn_state == "alive_responded", r.conn_state
        fields = decode_raw(r.response_bytes)
        assert fields and fields[0]["field"] == 1 and fields[0]["value"] == SERVER_N, fields
        print(f"  [ok] Ping -> OK, n={fields[0]['value']}, latency={r.latency_ms:.1f}ms")

        # 2) application error must NOT look like a crash
        r = probe.invoke(f"/{SVC}/Boom", b"")
        assert not r.ok and r.status_code == "INVALID_ARGUMENT", r.to_dict()
        assert r.conn_state == "alive_responded", r.conn_state
        print(f"  [ok] Boom -> INVALID_ARGUMENT classified alive_responded "
              f"(msg={r.grpc_message!r})")

        # 3) unimplemented method -> still alive
        r = probe.invoke(f"/{SVC}/NoSuchMethod", b"")
        assert r.status_code == "UNIMPLEMENTED", r.status_code
        assert r.conn_state == "alive_responded", r.conn_state
        print("  [ok] unknown method -> UNIMPLEMENTED, alive_responded")
        chan.close()

        # 4) unreachable port
        dead = build_channel("127.0.0.1:1", plaintext=True)
        r = Probe(dead, "127.0.0.1:1", "plaintext").invoke(f"/{SVC}/Ping", b"", timeout=2)
        assert not r.ok and r.conn_state in ("unreachable", "connection_reset"), r.to_dict()
        print(f"  [ok] closed port -> {r.status_code}/{r.conn_state}")
        dead.close()

        # 5) casebook record + read round-trip
        cbdir = tempfile.mkdtemp(prefix="grpcpoke_selftest_")
        cb = Casebook(cbdir)
        rid = cb.record_request(target=target, transport="plaintext",
                                method=f"/{SVC}/Ping", payload=b"", metadata=[("x-test", "1")])
        chan = build_channel(target, plaintext=True)
        res = Probe(chan, target, "plaintext").invoke(
            f"/{SVC}/Ping", b"", metadata=[("x-test", "1")], request_id=rid)
        cb.record_result(res)
        chan.close()
        rec = cb.get_request(rid)
        assert rec["method"].endswith("/Ping") and rec["metadata"][0][0] == "x-test", rec
        print(f"  [ok] casebook record+read round-trip (id={rid}, dir={cbdir})")

        # 6) typed path via an in-memory descriptor set (no protoc)
        sch = Schema(_selftest_protoset())
        assert f"/{SVC}/Ping" in sch.list_methods(), sch.list_methods()
        assert sch.find_method("Ping") == f"/{SVC}/Ping", sch.find_method("Ping")
        enc = sch.encode_json(f"/{SVC}/Ping", '{"n": "42"}')
        assert decode_raw(enc)[0]["value"] == 42, decode_raw(enc)
        js = sch.decode_response(f"/{SVC}/Ping", PING_REPLY)
        assert str(SERVER_N) in js, js
        print(f"  [ok] protoset encode/decode + list/find methods (enc={enc.hex()})")

        print("ALL SELFTESTS PASSED")
    except AssertionError as e:
        ok = False
        print(f"  [FAIL] {e}")
    finally:
        server.stop(0)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
