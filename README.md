# gRPC Poke

`grpcpoke` — a gRPC **send → observe → replay** harness for authorized testing
of a gRPC endpoint when you have the address but not the source.

It is **not** a fuzzer — it generates no payloads. *You* bring the bytes (from
your own mutator: boofuzz / atheris / radamsa / a shell loop); it sends them,
records exactly what came back, and can replay any request byte-for-byte. Its
value is:

- **Observe.** Every send returns a structured result: gRPC status code +
  message, trailing metadata, response bytes, latency, and an inferred
  **connection state** — so a normal `INVALID_ARGUMENT` rejection is never
  confused with a crash, reset, GOAWAY, or hang.
- **Replay.** Every request is flushed to disk (a *casebook*) **before** it is
  sent, so any payload that trips a bug is reproducible byte-for-byte.

### Why no `.proto` is needed

The core path uses gRPC identity serializers: you give a **method path** and
**raw bytes**, and the gRPC runtime handles TLS/mTLS, HTTP/2, and framing.
Method paths travel on the wire (`:path`) of every call anyway, so naming one
reveals nothing new. No message schema is embedded. Typed encode/decode
(`--json`, typed `decode`) is available **only** when *you* supply a compiled
descriptor set (`--protoset`) at runtime.

## Setup

A self-contained, offline bundle — bundled Python, no install step, no package
manager, no internet. Unpack it and run:

```bash
tar xzf grpcpoke-*.tar.gz
cd grpcpoke
./grpcpoke --help
./grpcpoke selftest      # offline end-to-end check, no server needed
```

Everything runs as `./grpcpoke <subcommand>`. The subcommands are `call`,
`replay`, `decode`, `list-methods`, and `selftest`.

## Transports

`call TARGET` takes a `HOST:PORT` target plus transport flags:

| Transport | Flags |
|-----------|-------|
| plaintext (no TLS) | `--plaintext` |
| mTLS | `--ca CA.pem --cert client.pem --key client.key` |

Omit `--cert`/`--key` for server-auth-only TLS (just `--ca`). Without
`--plaintext` the channel is TLS by default. `--max-msg` sets the max send/recv
message size (default 64 MiB).

`--server-name` overrides the TLS target-name (and the HTTP/2 `:authority`) used
to verify the server certificate. **It has no default** — set it only when the
certificate's CN/SAN does not match the address you dial; otherwise the
handshake fails on a name mismatch.

## Quickstart — connectivity check

Send an empty body to a known method path and inspect the outcome. A full path
with `--method` always works, with no schema or name resolution:

```bash
./grpcpoke call HOST:PORT --plaintext \
    --method /example.v1.Greeter/SayHello --empty
```

```
[OK ] /example.v1.Greeter/SayHello
      id=000000  target=HOST:PORT (plaintext)  latency=2.4ms
      status=OK(0)  conn_state=alive_responded
      request=0B  response=7B
      response (raw-decode):
        [
          {
            "field": 1,
            "wire": "len",
            "len": 5,
            "string": "hello"
          }
        ]
      recorded: ./casebook/requests.jsonl (id=000000)
```

`status=OK` with `conn_state=alive_responded` proves the transport, TLS/framing,
and the method path work end-to-end. An application rejection (e.g.
`INVALID_ARGUMENT`) is *also* `alive_responded` — see
[Connection states](#connection-states) for how the other outcomes read.

## Raw payloads — bring your own bytes

The primary mode: send arbitrary bytes to a method path, no schema required.
Give exactly one payload source — `--empty`, `--data-file`, `--data-hex`, or
`--data-b64`:

```bash
./grpcpoke call HOST:PORT --plaintext \
    --method /pkg.Service/Method --data-file ./mutated.bin

./grpcpoke call HOST:PORT --plaintext \
    --method /pkg.Service/Method --data-hex 0a0568656c6c6f
```

Pipe in bytes from your mutator and keep the casebook as your evidence + repro
log. Add gRPC metadata with `--metadata key=value` (repeatable; a key ending in
`-bin` takes a base64 value) and a deadline with `--timeout SECONDS`.

## Method selection

A method is identified by its full path `/pkg.Service/Method`. Two ways to name
it:

- `--method /pkg.Service/Method` — full path, always works, no source needed.
- `--rpc <RpcName>` — a short name, resolved to a full path from a source:
  - `--service pkg.Service` — prefixes the short name
    (`--rpc SayHello --service example.v1.Greeter` → `/example.v1.Greeter/SayHello`), or
  - `--methods FILE` — a file of full paths; the short name is matched by a
    unique suffix, or
  - `--protoset FILE` — resolves against the methods defined in a descriptor set.

Resolution fails if the short name is unknown or ambiguous. A `--methods` file
is one full path per line; blank lines and `#` comments are ignored — see
`examples/example.methods.txt`:

```
/example.v1.Greeter/SayHello
/example.v1.Greeter/SayGoodbye
```

List the method paths a source knows about:

```bash
./grpcpoke list-methods --methods examples/example.methods.txt
./grpcpoke list-methods --protoset service.protoset
```

`list-methods` has no built-in catalog; it errors without `--methods` or
`--protoset`.

## Typed calls and decode (optional — needs a protoset)

Everything above is schema-free. If you were provided a compiled descriptor set
(a `.protoset` / `FileDescriptorSet`), pass it with `--protoset` to send
requests written as JSON and to typed-decode responses. You do **not** build
this file — use the one you were handed:

```bash
# send a JSON request (requires --protoset)
./grpcpoke call HOST:PORT --plaintext --protoset service.protoset \
    --rpc <RpcName> --json '{"some_field":"value"}'

# typed-decode captured bytes as a message type, or as an RPC's response
./grpcpoke decode --data-hex 0a... --protoset service.protoset --as-type pkg.SomeMessage
./grpcpoke decode --data-hex 0a... --protoset service.protoset --as-response <RpcName>

# schema-free structural view (field numbers, wire types, nested messages)
./grpcpoke decode --data-hex 0a...
```

Proto `bytes` fields are carried as base64 in JSON, both directions. Without a
protoset, `decode` still gives the structural view and `call` still raw-decodes
every response.

## Replay and casebook

Every `call` and `replay` is recorded under `--casebook DIR` (default
`./casebook`): the request is written and **fsync'd to `requests.jsonl` before
the send**, so a payload that hangs or crashes the target stays reproducible;
the outcome lands in `results.jsonl` afterward. `--no-store` disables recording;
`--out-json` prints the machine-readable result instead of the text summary.

Re-send any recorded request byte-for-byte by id:

```bash
./grpcpoke replay 000042                       # same target + transport

./grpcpoke replay 000042 --target HOST:PORT \
    --ca CA.pem --cert client.pem --key client.key   # retarget / change transport
```

mTLS key material is never stored, so replaying a request that used mTLS (or
retargeting one at an mTLS endpoint) re-supplies `--ca/--cert/--key`. Point
`--casebook` at a different directory to keep engagements separate.

## Connection states

Every result carries a `conn_state`, inferred from the status code and the
transport error text. The key distinction: a normal application rejection is
`alive_responded`, not a failure state.

| state | meaning |
|-------|---------|
| `alive_responded` | server received the RPC and returned a status — OK **or** an app error (INVALID_ARGUMENT, UNAUTHENTICATED, NOT_FOUND, …) |
| `deadline` | client deadline hit — server slow or hung |
| `connection_reset` | dropped mid-RPC (RST_STREAM / socket closed / broken pipe) — possible crash |
| `goaway` | server sent an HTTP/2 GOAWAY |
| `handshake_failed` | TCP up but the TLS/mTLS handshake or cert check failed |
| `unreachable` | never connected (refused / DNS / no route) |
| `unknown` | couldn't classify — inspect `debug_error_string` |

## Gotchas

- **Server cert name mismatch → set `--server-name`.** If the certificate's
  CN/SAN doesn't match the address you dial, TLS verification fails
  (`conn_state=handshake_failed`). Pass `--server-name` with the name the
  certificate actually presents.
- **Valid chain ≠ authorized.** A client certificate whose chain verifies but
  whose identity the server doesn't authorize can still return
  `UNAUTHENTICATED`. That reads as `alive_responded` (not `handshake_failed`),
  so you can tell "auth denied" apart from "TLS broke."
- **`--json` requires `--protoset`.** Without a descriptor set, use a raw
  payload (`--data-file`/`--data-hex`/`--data-b64`).
- **`--rpc` needs a resolution source.** Give `--service`, `--methods FILE`, or
  `--protoset` — or just use the full `--method` path.
- **Plaintext may be logged or alerted.** Some servers record or raise an alert
  on insecure/plaintext connections. It isn't blocked, but plan your engagement
  notes accordingly.

## Files

```
grpcpoke/
  grpcpoke        launcher — run everything as ./grpcpoke <subcommand>
  grpc_poke/       the tool (cli / channel / client / result / store / codec / methods)
  selftest.py       offline end-to-end self-test
  examples/         sample inputs (e.g. example.methods.txt)
  runtime/          bundled Python interpreter (do not edit)
  vendor/           bundled dependencies (do not edit)
```
