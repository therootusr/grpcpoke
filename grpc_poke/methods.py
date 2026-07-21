"""Method-path helpers for a generic gRPC target.

No service or method list is baked in. A method is either given as a full path
("/pkg.Service/Method") or resolved from a short RPC name using an explicit
`--service`, a `--methods` file, or the method list from a supplied protoset.
Full method paths travel on the wire (`:path`) of every call regardless, so a
methods file carries nothing that a capture wouldn't.
"""
from __future__ import annotations

from typing import List, Optional


def full_path(name_or_path: str, service: Optional[str] = None) -> str:
    """Build a full "/pkg.Service/Method" path.

    A value already starting with "/" is passed through. Otherwise a `service`
    (fully-qualified, e.g. "pkg.Service") is required to prefix the short name.
    """
    s = name_or_path.strip()
    if s.startswith("/"):
        return s
    short = s.rsplit("/", 1)[-1]
    if service:
        return f"/{service.strip().strip('/')}/{short}"
    raise ValueError(
        "need a full '/pkg.Service/Method' path or a --service to build one")


def read_methods_file(path: str) -> List[str]:
    """Read a methods file: one method path per line; blank lines and lines
    starting with '#' are ignored. Bare "pkg.Service/Method" gets a leading '/'.
    """
    out: List[str] = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line if line.startswith("/") else "/" + line)
    return out


def resolve_rpc(name: str,
                methods: Optional[List[str]] = None,
                service: Optional[str] = None) -> str:
    """Resolve a short RPC name to a full path.

    Precedence: an already-full path wins; then `--service` prefixing; then a
    unique suffix match against `methods`. Raises ValueError if it can't resolve
    or the short name is ambiguous.
    """
    s = name.strip()
    if s.startswith("/"):
        return s
    if service:
        return full_path(s, service=service)
    if methods:
        short = s.rsplit("/", 1)[-1]
        matches = [m for m in methods if m.rsplit("/", 1)[-1] == short]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"no method matching {short!r} in the methods list")
        raise ValueError(f"ambiguous method {short!r}; matches: {matches}")
    raise ValueError(
        "give a full --method path, or --service, or --methods FILE (or "
        "--protoset) to resolve an --rpc name")
