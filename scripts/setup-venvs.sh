#!/usr/bin/env bash
#
# This repository's own Python environments. Previously it borrowed askfelix's,
# which meant a dependency change in an unrelated project could break this one
# and neither repo recorded what the other needed.
#
#   ./scripts/setup-venvs.sh            both environments
#   ./scripts/setup-venvs.sh --runtime  only .venv     (3.12, the server)
#   ./scripts/setup-venvs.sh --tests    only .venv-13  (3.13, pytest and bench)
#
# Two of them, and the reason is the one thing in this repo that confuses
# everybody: `pixi run mojo build` produces a binary linked against **CPython
# 3.12**, so the running server needs 3.12 site-packages, while `pixi run
# python` is **3.13** and the test suite needs 3.13 site-packages. Point either
# at the other and numpy fails with "you should not try to import numpy from
# its source directory", which is not what is wrong.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYBED="${PYBED_DIR:-$ROOT/../pybed}"
MOJOSUB="${MOJOSUB_DIR:-$ROOT/../mojosub}"
MOJOPLOTLY="${MOJOPLOTLY_DIR:-$ROOT/../mojo-plotly}"
GOBED="${GOBED_DIR:-$ROOT/../gobed}"
MODEL_DIR="$ROOT/models/embed"

DO_RUNTIME=1
DO_TESTS=1
for arg in "$@"; do
  case "$arg" in
    --runtime) DO_TESTS=0 ;;
    --tests)   DO_RUNTIME=0 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die()  { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

command -v uv >/dev/null || die "uv is not installed (https://docs.astral.sh/uv/)"

build_env() {
  local path="$1" version="$2" dev="${3:-0}"
  say "Building $path (python $version)"
  uv venv --python "$version" "$path"
  uv pip install --python "$path/bin/python" -r requirements.txt
  # The test environment gets pytest and a browser driver; the runtime one
  # must not - a server that can launch chromium is a server that will.
  [ "$dev" = 1 ] && uv pip install --python "$path/bin/python" -r requirements-dev.txt

  # pybed is a sibling checkout, not a published package: static int8
  # embeddings whose only dependency is numpy.
  #
  # NOT editable. An editable install is a `.pth` file plus an import finder,
  # and `.pth` files are only executed by `site` when it processes a
  # site-packages directory. The server is an embedded interpreter that adds
  # this path with `sys.path.insert` (server/th/py.mojo), which runs no `.pth`
  # at all - so an editable pybed is importable from the venv's own python and
  # invisible to the thing that actually needs it. Re-run this script to pick
  # up changes in ../pybed.
  if [ -d "$PYBED" ]; then
    uv pip install --python "$path/bin/python" --reinstall-package pybed "$PYBED"
  else
    warn "no pybed checkout at $PYBED - the fast classifier will stay off"
  fi

  # mojosub compiles the agent's hot numeric functions to Mojo. Same sibling
  # checkout, same not-editable reason as pybed. Absent, the interpreter runs
  # everything in CPython exactly as before.
  if [ -d "$MOJOSUB" ]; then
    uv pip install --python "$path/bin/python" --reinstall-package mojosub "$MOJOSUB"
  else
    warn "no mojosub checkout at $MOJOSUB - interpreter acceleration stays off"
  fi

  # mojo-plotly supplies the LTTB downsample that keeps a million-row line
  # under 4000 points. Its shared library is built HERE, not on first use:
  # `mojoplotly` compiles lazily, and a five-second `mojo build` inside a
  # request is not acceptable. charts/decimate.py checks the .so exists and
  # falls back to a stride rather than triggering a build.
  if [ -d "$MOJOPLOTLY" ]; then
    uv pip install --python "$path/bin/python" --reinstall-package mojoplotly "$MOJOPLOTLY"
    # Build in the checkout (which has the sources) and copy the result next
    # to the installed package. A non-editable install has no `src/`, so the
    # library has to travel with it; `decimate.available()` looks here.
    if (cd "$MOJOPLOTLY" && pixi run mojo build --emit shared-lib -I src src/capi.mojo -o build/capi.so >/dev/null 2>&1); then
      local site
      site="$("$path/bin/python" -c 'import mojoplotly,os;print(os.path.dirname(mojoplotly.__file__))')"
      cp "$MOJOPLOTLY/build/capi.so" "$site/capi.so"
    else
      warn "mojo-plotly kernel would not build - charts use the stride fallback"
    fi
  else
    warn "no mojo-plotly checkout at $MOJOPLOTLY - charts use the stride fallback"
  fi
}

fetch_model() {
  # The embedding weights live in gobed and pybed, both of which have the same
  # int8 512-dimension file. Copy rather than symlink: the deployed server must
  # not depend on a sibling checkout still being there.
  say "Embedding model"
  mkdir -p "$MODEL_DIR"

  local copied=0
  for source in "$GOBED/model" "$PYBED/model"; do
    if [ -f "$source/modelint8_512dim.safetensors" ]; then
      for file in modelint8_512dim.safetensors tokenizer.json; do
        if [ ! -f "$MODEL_DIR/$file" ]; then
          cp "$source/$file" "$MODEL_DIR/$file"
          echo "    $file <- $source"
        fi
      done
      copied=1
      break
    fi
  done

  if [ "$copied" = 0 ]; then
    warn "no model found in $GOBED/model or $PYBED/model"
    warn "the classifier falls back to its lexical rules, which is a supported state"
  fi
}

[ "$DO_RUNTIME" = 1 ] && build_env "$ROOT/.venv" "3.12"
[ "$DO_TESTS" = 1 ] && build_env "$ROOT/.venv-13" "3.13" 1
fetch_model

say "Done"
cat <<EOF
    server   TWOHELIXES_SITE_PACKAGES=$ROOT/.venv/lib/python3.12/site-packages
    tests    PYTHONPATH=interp:$ROOT/.venv-13/lib/python3.13/site-packages
    model    $MODEL_DIR
EOF
