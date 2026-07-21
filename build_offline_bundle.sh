#!/usr/bin/env bash
# Build a self-sufficient OFFLINE bundle of grpcpoke for linux/x86_64.
#
# The bundle carries its own standalone CPython and its vendored dependencies
# (grpcio + protobuf), so the recipient untars it and runs ./grpcpoke
# with NO install step, NO package manager, and NO internet. This script is the
# only thing that needs uv + internet, and it runs elsewhere (not shipped).
#
#   ./build_offline_bundle.sh [--out DIR] [--python-version X.Y] [--no-slim]
#
# Requires: uv (built as a static binary), internet, x86_64 linux.
set -euo pipefail

# --- config -----------------------------------------------------------------
PY_VER="3.12"
GRPCIO_VER="1.82.1"        # keep in lock-step with uv.lock
PROTOBUF_VER="7.35.1"      # keep in lock-step with uv.lock
PY_PLATFORM="x86_64-manylinux_2_17"   # glibc-2.17 floor -> runs on old userlands
OUT="/tmp"
SLIM=1                    # drop unused stdlib (test/idlelib/...) to save space

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --python-version) PY_VER="$2"; shift 2 ;;
    --no-slim) SLIM=0; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR=$(cd -P "$(dirname "$0")" && pwd)
SRC="$SCRIPT_DIR"
NAME="grpcpoke"

UV=$(command -v uv || echo "$HOME/.local/bin/uv")
[ -x "$UV" ] || { echo "error: uv not found (need uv + internet on this build box)" >&2; exit 1; }
[ "$(uname -m)" = "x86_64" ] || { echo "error: this build targets x86_64; run on an x86_64 box" >&2; exit 1; }

# --- guard: pinned versions must match uv.lock ------------------------------
if [ -f "$SRC/uv.lock" ]; then
  grep -q "version = \"$GRPCIO_VER\"" "$SRC/uv.lock" \
    && grep -q "version = \"$PROTOBUF_VER\"" "$SRC/uv.lock" \
    || { echo "error: pinned grpcio/protobuf versions not in uv.lock — update this script" >&2; exit 1; }
fi

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
BUNDLE="$BUILD/bundle/$NAME"
mkdir -p "$BUNDLE"

echo "==> [1/7] fetching standalone CPython $PY_VER"
UV_PYTHON_INSTALL_DIR="$BUILD/py" UV_PYTHON_INSTALL_BIN=0 "$UV" python install --install-dir "$BUILD/py" "$PY_VER"
PYREAL=$(UV_PYTHON_INSTALL_DIR="$BUILD/py" "$UV" python find --managed-python --no-project --resolve-links "$PY_VER")
PYROOT=$(dirname "$(dirname "$PYREAL")")
echo "    python: $PYROOT"

echo "==> [2/7] copying runtime into the bundle"
mkdir -p "$BUNDLE/runtime"
cp -a "$PYROOT" "$BUNDLE/runtime/python"   # -> runtime/python/{bin,lib,...}; relocatable via $ORIGIN/../lib
BPY="$BUNDLE/runtime/python/bin/python3"

echo "==> [3/7] vendoring grpcio==$GRPCIO_VER protobuf==$PROTOBUF_VER (uv, offline-ready)"
# pip interface (not `uv sync`) on purpose: we're not building the project env,
# we're vendoring wheels into a flat, relocatable --target dir (bundle runs off
# PYTHONPATH, not a venv) for the bundled interpreter + a fixed old-glibc
# platform (--python/--python-platform) — none of which `uv sync` can do (it
# only makes a venv resolved for THIS host). That bypasses uv.lock's resolver,
# so grpcio/protobuf are pinned by hand here; the guard above asserts they match uv.lock.
"$UV" pip install \
  --python "$BPY" \
  --target "$BUNDLE/vendor" \
  --python-platform "$PY_PLATFORM" \
  --only-binary :all: --link-mode copy --no-cache \
  "grpcio==$GRPCIO_VER" "protobuf==$PROTOBUF_VER"
rm -f "$BUNDLE/vendor/.lock"

echo "==> [4/7] copying tool sources (tool-only; no protoset/methods/casebook/venv/lock)"
cp -a "$SRC/grpc_poke" "$BUNDLE/"
cp -a "$SRC/selftest.py" "$SRC/README.md" "$BUNDLE/"
rm -rf "$BUNDLE/grpc_poke/__pycache__"

echo "==> [5/7] writing launcher"
cat > "$BUNDLE/$NAME" <<'LAUNCHER'
#!/bin/sh
# Resolve our own dir (realpath follows symlinks; spaces-safe) and exec the
# bundled standalone CPython with the tool package + vendored deps on the path.
set -eu
HERE=$(dirname "$(realpath "$0")")
export PYTHONPATH="$HERE/vendor:$HERE${PYTHONPATH:+:$PYTHONPATH}"
exec "$HERE/runtime/python/bin/python3" -s -m grpc_poke "$@"
LAUNCHER
chmod 755 "$BUNDLE/$NAME"

if [ "$SLIM" = 1 ]; then
  echo "==> [5b] slimming unused stdlib + pip/setuptools"
  L="$BUNDLE/runtime/python/lib/python$PY_VER"
  for d in test idlelib lib2to3 tkinter turtledemo ensurepip; do rm -rf "$L/$d" 2>/dev/null || true; done
  # the tool imports only grpc + google.protobuf (from vendor/) + stdlib; drop pip/setuptools.
  rm -rf "$L/site-packages/pip" "$L/site-packages"/pip-*.dist-info \
         "$L/site-packages/setuptools" "$L/site-packages"/setuptools-*.dist-info \
         "$L/site-packages/pkg_resources" "$L/site-packages/_distutils_hack" \
         "$L/site-packages/distutils-precedence.pth" 2>/dev/null || true
  rm -f "$BUNDLE"/runtime/python/bin/pip* "$BUNDLE"/runtime/python/bin/idle* "$BUNDLE"/runtime/python/bin/2to3* 2>/dev/null || true
fi

echo "==> [6/7] sanity gate (offline: scrubbed env, no uv, no system python)"
run_clean() { env -i PATH=/usr/bin:/bin "$@"; }
run_clean "$BUNDLE/$NAME" --help >/dev/null || { echo "    FAIL: --help" >&2; exit 1; }
run_clean "$BUNDLE/$NAME" selftest >/dev/null || { echo "    FAIL: selftest" >&2; exit 1; }
run_clean env PYTHONPATH="$BUNDLE/vendor" "$BPY" -c "import grpc, google.protobuf" \
  || { echo "    FAIL: import check" >&2; exit 1; }
echo "    ok: --help + selftest + import"

# drop bytecode caches the sanity gate just generated, so they don't ship
find "$BUNDLE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> [7/7] packaging"
mkdir -p "$OUT"
DATE=$(date +%Y%m%d)
TARBALL="$NAME-offline-linux-x86_64-$DATE.tar.gz"
tar -C "$BUILD/bundle" -czf "$OUT/$TARBALL" "$NAME"
echo
echo "bundle:  $OUT/$TARBALL"
echo "size:    $(du -h "$OUT/$TARBALL" | cut -f1)"
echo "sha256:  $(sha256sum "$OUT/$TARBALL" | cut -d' ' -f1)"
