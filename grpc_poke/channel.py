"""gRPC channel construction for the two transports.

  * secure/mTLS  -> ssl_channel_credentials(root=CA, key=client_key, cert=client_cert).
                    If the server certificate's CN/SAN doesn't match the connect
                    address, pass `server_name` to override the TLS target-name
                    (and :authority) so verification uses that name instead.
  * insecure     -> plaintext, no credentials.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import grpc

_DEFAULT_MAX_MSG = 64 * 1024 * 1024  # some RPC payloads can be large.


def _read(path: Optional[str]) -> Optional[bytes]:
    if not path:
        return None
    with open(path, "rb") as f:
        return f.read()


def build_channel(
    target: str,
    *,
    plaintext: bool,
    ca: Optional[str] = None,
    cert: Optional[str] = None,
    key: Optional[str] = None,
    server_name: Optional[str] = None,
    max_msg: int = _DEFAULT_MAX_MSG,
    extra_options: Optional[List[Tuple[str, object]]] = None,
) -> grpc.Channel:
    """Return a grpc.Channel for `target` ("host:port").

    plaintext=True  -> insecure channel.
    plaintext=False -> TLS/mTLS channel. Pass `ca` (verify server) and, for
                       mutual TLS, `cert`+`key` (our client identity). Pass
                       `server_name` to override the verified name/authority.
    """
    options: List[Tuple[str, object]] = list(extra_options or [])
    options += [
        ("grpc.max_receive_message_length", max_msg),
        ("grpc.max_send_message_length", max_msg),
    ]
    if not plaintext and server_name:
        # ssl_target_name_override: verify the server cert against this name.
        # default_authority: send it as the HTTP/2 :authority too.
        options += [
            ("grpc.ssl_target_name_override", server_name),
            ("grpc.default_authority", server_name),
        ]

    if plaintext:
        return grpc.insecure_channel(target, options=options)

    creds = grpc.ssl_channel_credentials(
        root_certificates=_read(ca),
        private_key=_read(key),
        certificate_chain=_read(cert),
    )
    return grpc.secure_channel(target, creds, options=options)
