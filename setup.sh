#!/usr/bin/env bash
# Bootstrap a complete twoHelixes development workspace.
#
# The lower-level Python split lives in scripts/setup-venvs.sh. This is the
# one command a fresh checkout needs: Python environments, frontend packages,
# production assets, and the Chromium binary used by e2e/visualbench.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DO_PYTHON=1
DO_WEB=1
DO_BROWSER=1
PYTHON_MODE="both"
DRY_RUN=0
CHECK_ONLY=0

usage() {
  cat <<'EOF'
Usage: ./setup.sh [options]

Options:
  --runtime       Build only the Python 3.12 server environment
  --tests         Build only the Python 3.13 test environment
  --skip-python   Do not create Python environments
  --skip-web      Do not install or build frontend assets
  --skip-browser  Do not install Playwright Chromium
  --check         Verify an existing setup without changing it
  --dry-run       Print the bootstrap commands without running them
  -h, --help      Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --runtime) PYTHON_MODE="runtime"; DO_BROWSER=0 ;;
    --tests) PYTHON_MODE="tests" ;;
    --skip-python) DO_PYTHON=0 ;;
    --skip-web) DO_WEB=0 ;;
    --skip-browser) DO_BROWSER=0 ;;
    --check) CHECK_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Error: unknown option: %s\n' "$arg" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '    '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

if [ "$CHECK_ONLY" = 1 ]; then
  missing=0
  check_command() {
    if command -v "$1" >/dev/null 2>&1; then
      printf 'ok       command %s\n' "$1"
    else
      printf 'missing  command %s\n' "$1"
      missing=1
    fi
  }
  check_path() {
    if [ -e "$ROOT/$1" ]; then
      printf 'ok       %s\n' "$1"
    else
      printf 'missing  %s\n' "$1"
      missing=1
    fi
  }

  [ "$DO_PYTHON" = 0 ] || check_command uv
  [ "$DO_WEB" = 0 ] || check_command bun
  [ "$DO_PYTHON" = 0 ] || check_command pixi

  if [ "$DO_PYTHON" = 1 ]; then
    [ "$PYTHON_MODE" = "tests" ] || check_path .venv/bin/python
    [ "$PYTHON_MODE" = "runtime" ] || check_path .venv-13/bin/python
  fi
  if [ "$DO_WEB" = 1 ]; then
    check_path web/node_modules
    check_path static/app.js
    check_path static/marketing.js
    check_path static/styles.css
  fi
  if [ "$DO_BROWSER" = 1 ] && [ "$DO_PYTHON" = 1 ] && [ "$PYTHON_MODE" != "runtime" ]; then
    if [ -x "$ROOT/.venv-13/bin/python" ] && \
       "$ROOT/.venv-13/bin/python" -m playwright install --dry-run chromium \
         >/dev/null 2>&1; then
      printf 'ok       Playwright Chromium metadata\n'
    else
      printf 'missing  Playwright Chromium metadata\n'
      missing=1
    fi
  fi
  exit "$missing"
fi

if [ "$DO_PYTHON" = 1 ]; then
  say "Python environments"
  case "$PYTHON_MODE" in
    runtime) run "$ROOT/scripts/setup-venvs.sh" --runtime ;;
    tests) run "$ROOT/scripts/setup-venvs.sh" --tests ;;
    both) run "$ROOT/scripts/setup-venvs.sh" ;;
  esac
fi

if [ "$DO_WEB" = 1 ]; then
  say "Frontend dependencies and assets"
  if [ "$DRY_RUN" = 1 ]; then
    printf '    cd %q\n' "$ROOT/web"
    run bun install --frozen-lockfile
    run bun run build
  else
    (cd "$ROOT/web" && bun install --frozen-lockfile && bun run build)
  fi
fi

if [ "$DO_BROWSER" = 1 ]; then
  say "Playwright Chromium"
  if [ "$DO_PYTHON" = 0 ] && [ ! -x "$ROOT/.venv-13/bin/python" ]; then
    printf 'Error: --skip-python needs an existing .venv-13 to install Chromium\n' >&2
    exit 1
  fi
  run "$ROOT/.venv-13/bin/python" -m playwright install chromium
fi

say "Setup complete"
printf '    verify  ./setup.sh --check\n'
printf '    tests   PYTHONPATH=interp ./.venv-13/bin/python -m pytest tests -q\n'
printf '    visual  PYTHONPATH=interp ./.venv-13/bin/python bench/visualbench.py --base URL\n'
