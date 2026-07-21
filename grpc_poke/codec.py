"""Two levels of protobuf decoding.

1. decode_raw(bytes)  -- schema-free structural decode (equivalent to protobuf's
   raw wire dump). Needs no schema and no protobuf runtime. Always available, so
   the caller can observe response/message structure (field numbers, wire types,
   nested msgs) without ever holding the schema.

2. Schema(protoset)   -- typed encode/decode from a compiled FileDescriptorSet
   (protoset). Used only when a protoset is supplied at runtime.
"""
from __future__ import annotations

import struct
from typing import List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Schema-free structural decode
# --------------------------------------------------------------------------- #

def _read_varint(buf: bytes, i: int) -> Tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _looks_printable(chunk: bytes) -> Optional[str]:
    """Return a decoded string if `chunk` is plausibly human text, else None."""
    if not chunk:
        return None
    try:
        s = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for c in s if c.isprintable() or c in "\t\n\r")
    return s if printable / max(1, len(s)) >= 0.85 else None


def _decode(data: bytes, depth: int) -> Tuple[list, bool]:
    """Return (fields, clean). `clean` is True iff the whole buffer parsed with
    no truncation/unknown wire types — used to decide if a LEN chunk is a
    nested message vs opaque bytes."""
    out: list = []
    i = 0
    n = len(data)
    clean = True
    while i < n:
        try:
            tag, i = _read_varint(data, i)
        except ValueError:
            out.append({"error": "truncated tag", "rest_hex": data[i:].hex()})
            return out, False
        field_no = tag >> 3
        wire = tag & 7
        if field_no == 0:
            out.append({"error": "zero field number", "rest_hex": data[i:].hex()})
            return out, False
        if wire == 0:  # varint
            try:
                v, i = _read_varint(data, i)
            except ValueError:
                out.append({"field": field_no, "wire": "varint", "error": "truncated"})
                return out, False
            out.append({"field": field_no, "wire": "varint", "value": v})
        elif wire == 1:  # 64-bit
            if i + 8 > n:
                out.append({"field": field_no, "wire": "i64", "error": "truncated"})
                return out, False
            raw = data[i:i + 8]
            i += 8
            out.append({"field": field_no, "wire": "i64",
                        "u64": struct.unpack("<Q", raw)[0],
                        "double": struct.unpack("<d", raw)[0]})
        elif wire == 2:  # length-delimited
            try:
                ln, i = _read_varint(data, i)
            except ValueError:
                out.append({"field": field_no, "wire": "len", "error": "truncated length"})
                return out, False
            if i + ln > n:
                out.append({"field": field_no, "wire": "len",
                            "error": "truncated value", "declared_len": ln})
                return out, False
            chunk = data[i:i + ln]
            i += ln
            entry = {"field": field_no, "wire": "len", "len": ln}
            text = _looks_printable(chunk)
            sub, sub_clean = ([], False)
            if ln > 0 and depth < 16:
                sub, sub_clean = _decode(chunk, depth + 1)
            if text is not None:
                entry["string"] = text
                if sub_clean and sub:
                    entry["maybe_message"] = sub
            elif sub_clean and sub:
                entry["message"] = sub
            else:
                entry["bytes_hex"] = chunk.hex()
            out.append(entry)
        elif wire == 5:  # 32-bit
            if i + 4 > n:
                out.append({"field": field_no, "wire": "i32", "error": "truncated"})
                return out, False
            raw = data[i:i + 4]
            i += 4
            out.append({"field": field_no, "wire": "i32",
                        "u32": struct.unpack("<I", raw)[0],
                        "float": struct.unpack("<f", raw)[0]})
        elif wire == 3:  # start group (proto2, deprecated)
            out.append({"field": field_no, "wire": "start_group"})
        elif wire == 4:  # end group
            out.append({"field": field_no, "wire": "end_group"})
        else:
            out.append({"field": field_no, "wire": wire, "error": "unknown wire type"})
            return out, False
    return out, clean


def decode_raw(data: bytes) -> list:
    """Schema-free structural decode of a protobuf message."""
    return _decode(bytes(data), 0)[0]


# --------------------------------------------------------------------------- #
# Optional typed layer (protoset)
# --------------------------------------------------------------------------- #

class Schema:
    """Typed encode/decode backed by a compiled FileDescriptorSet (protoset)."""

    def __init__(self, protoset_bytes: bytes):
        from google.protobuf import descriptor_pb2, descriptor_pool

        fds = descriptor_pb2.FileDescriptorSet()
        fds.ParseFromString(protoset_bytes)
        pool = descriptor_pool.DescriptorPool()
        # Add files in dependency order (protoset order isn't guaranteed).
        pending = list(fds.file)
        added = set()
        progress = True
        while pending and progress:
            progress = False
            still: list = []
            for fdp in pending:
                if all(dep in added for dep in fdp.dependency):
                    pool.Add(fdp)
                    added.add(fdp.name)
                    progress = True
                else:
                    still.append(fdp)
            pending = still
        for fdp in pending:  # best effort if a dep is missing/cyclic
            pool.Add(fdp)
        self.pool = pool
        # Full method paths, computed straight from the descriptor set (the pool
        # has no "enumerate all services" API).
        self._methods: List[str] = []
        for fdp in fds.file:
            for svc in fdp.service:
                fqn = f"{fdp.package}.{svc.name}" if fdp.package else svc.name
                for m in svc.method:
                    self._methods.append(f"/{fqn}/{m.name}")

    def list_methods(self) -> List[str]:
        """All "/pkg.Service/Method" paths defined in the protoset."""
        return list(self._methods)

    def find_method(self, name_or_path: str) -> str:
        """Resolve a short RPC name (or full path) to a full path via the
        protoset's method list. Raises ValueError if absent or ambiguous."""
        s = name_or_path.strip()
        if s.startswith("/"):
            return s
        short = s.rsplit("/", 1)[-1]
        matches = [m for m in self._methods if m.rsplit("/", 1)[-1] == short]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"no method {short!r} in the protoset")
        raise ValueError(f"ambiguous method {short!r}; matches: {matches}")

    @staticmethod
    def _split(method_path: str) -> Tuple[str, str]:
        p = method_path.strip("/")
        svc, _, meth = p.rpartition("/")
        return svc, meth

    def _method(self, method_path: str):
        svc_name, meth = self._split(method_path)
        return self.pool.FindServiceByName(svc_name).FindMethodByName(meth)

    @staticmethod
    def _msg_class(desc):
        try:
            from google.protobuf import message_factory
            if hasattr(message_factory, "GetMessageClass"):
                return message_factory.GetMessageClass(desc)          # protobuf >= 4.22
            return message_factory.MessageFactory().GetPrototype(desc)  # older
        except Exception:
            from google.protobuf import symbol_database
            return symbol_database.Default().GetSymbol(desc.full_name)

    def encode_json(self, method_path: str, json_str: str) -> bytes:
        from google.protobuf import json_format
        msg = self._msg_class(self._method(method_path).input_type)()
        json_format.Parse(json_str, msg)
        return msg.SerializeToString()

    def decode_response(self, method_path: str, data: bytes) -> str:
        from google.protobuf import json_format
        msg = self._msg_class(self._method(method_path).output_type)()
        msg.ParseFromString(bytes(data))
        return json_format.MessageToJson(msg, preserving_proto_field_name=True)

    def decode_message(self, type_full_name: str, data: bytes) -> str:
        from google.protobuf import json_format
        desc = self.pool.FindMessageTypeByName(type_full_name.lstrip("."))
        msg = self._msg_class(desc)()
        msg.ParseFromString(bytes(data))
        return json_format.MessageToJson(msg, preserving_proto_field_name=True)
