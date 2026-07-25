#!/usr/bin/env bash
#
# Build, publish static assets to R2, and roll the twoHelixes service.
#
#   ./deploy.sh              build + upload + restart + verify
#   ./deploy.sh --assets     static assets only (no service restart)
#   ./deploy.sh --no-upload  build + restart, skip R2
#   ./deploy.sh --dry-run    show what would happen
#
# Credentials come from the environment or ~/.secretbashrc. Nothing secret is
# stored in this file: the account and zone identifiers below are not secrets,
# but R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are and must never be committed.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# --- configuration ---------------------------------------------------------

CF_ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID:-f76d25b8b86cfa5638f43016510d8f77}"
CF_ZONE_ID="${TWOHELIXES_ZONE_ID:-8d65880a1e4c353158a8090a353faf79}"

R2_BUCKET="${R2_BUCKET:-twohelixesstatic}"
R2_ENDPOINT="${R2_ENDPOINT:-https://${CF_ACCOUNT_ID}.r2.cloudflarestorage.com}"
R2_PUBLIC_HOST="${R2_PUBLIC_HOST:-twohelixesstatic.twohelixes.com}"

SERVICE="twohelixes"
LOCAL_URL="http://127.0.0.1:${TWOHELIXES_PORT:-7474}"
PUBLIC_URL="${TWOHELIXES_SITE_URL:-https://twohelixes.com}"

DO_UPLOAD=1
DO_SERVICE=1
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --assets)    DO_SERVICE=0 ;;
    --no-upload) DO_UPLOAD=0 ;;
    --dry-run)   DRY_RUN=1 ;;
    -h|--help)   sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die()  { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then echo "    [dry-run] $*"; else "$@"; fi; }

# Load secrets without echoing them. Errors are swallowed on purpose: that
# file is written for interactive shells and ends with things like `nvm use`
# that are undefined here - we only want its exports, not its side effects.
# Never run this script under `bash -x`; the trace would print every key.
if [ -f "$HOME/.secretbashrc" ]; then
  set +u +e
  # shellcheck disable=SC1091
  . "$HOME/.secretbashrc" >/dev/null 2>&1 || true
  set -u -e
fi

# --- build -----------------------------------------------------------------

say "Building frontend"
command -v bun >/dev/null || die "bun is not installed"
run bash -c 'cd web && bun install --frozen-lockfile 2>/dev/null || bun install'
run bash -c 'cd web && bun run build'

if [ "$DO_SERVICE" = 1 ]; then
  say "Building Mojo server"
  command -v pixi >/dev/null || die "pixi is not installed"
  # Build to a temp path first: a failed build must not leave the service
  # pointing at a half-written binary.
  run bash -c 'cd server && pixi run mojo build main.mojo -o ../build/twohelixes-server.new'
  if [ "$DRY_RUN" = 0 ]; then
    [ -s build/twohelixes-server.new ] || die "build produced no binary"
    mv build/twohelixes-server.new build/twohelixes-server
  fi
fi

say "Running tests"
if [ "$DRY_RUN" = 0 ]; then
  PYTHONPATH="interp:${TWOHELIXES_SITE_PACKAGES_TEST:-/nvme0n1-disk/code/askfelix/.venv-14/lib/python3.13/site-packages}" \
    pixi run python -m pytest tests -q || die "tests failed; not deploying"
else
  echo "    [dry-run] pytest tests -q"
fi

# --- R2 --------------------------------------------------------------------

upload_r2() {
  local key_id="${R2_ACCESS_KEY_ID:-${CLOUDFLARE_R2_ACCESS_KEY_ID:-}}"
  local secret="${R2_SECRET_ACCESS_KEY:-${CLOUDFLARE_R2_SECRET_ACCESS_KEY:-}}"

  if [ -z "$key_id" ] || [ -z "$secret" ]; then
    warn "R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY not set - skipping upload."
    warn "Add them to ~/.secretbashrc to publish assets to $R2_PUBLIC_HOST."
    return 0
  fi
  command -v aws >/dev/null || { warn "aws CLI not found - skipping R2 upload"; return 0; }

  say "Uploading static assets to r2://$R2_BUCKET"

  export AWS_ACCESS_KEY_ID="$key_id"
  export AWS_SECRET_ACCESS_KEY="$secret"
  export AWS_DEFAULT_REGION=auto
  export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
  export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required

  # Hashed chunks are immutable; entry points and art are not, so they get a
  # short TTL and are revalidated.
  run aws s3 sync static/ "s3://$R2_BUCKET/" \
    --endpoint-url "$R2_ENDPOINT" \
    --exclude '*' --include 'chunk-*' \
    --cache-control 'public, max-age=31536000, immutable' \
    --no-progress

  run aws s3 sync static/ "s3://$R2_BUCKET/" \
    --endpoint-url "$R2_ENDPOINT" \
    --exclude 'chunk-*' \
    --cache-control 'public, max-age=300, must-revalidate' \
    --delete --no-progress
}

purge_cache() {
  local token="${CLOUDFLARE_API_TOKEN:-${CLOUDFLARE_API_KEY:-}}"
  [ -n "$token" ] || { warn "no CLOUDFLARE_API_TOKEN - skipping cache purge"; return 0; }
  [ -n "$CF_ZONE_ID" ] || { warn "no zone id - skipping cache purge"; return 0; }

  say "Purging Cloudflare cache for zone $CF_ZONE_ID"
  if [ "$DRY_RUN" = 1 ]; then
    echo "    [dry-run] purge zone $CF_ZONE_ID"
    return 0
  fi
  curl -fsS -X POST \
    "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/purge_cache" \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    --data '{"purge_everything":true}' >/dev/null && echo "    purged"
}

[ "$DO_UPLOAD" = 1 ] && upload_r2

# --- service ---------------------------------------------------------------

if [ "$DO_SERVICE" = 1 ]; then
  say "Restarting $SERVICE"
  if [ "$DRY_RUN" = 0 ]; then
    sudo systemctl restart "$SERVICE"
    # The worker imports pandas and warms the interpreter, so give it room.
    for i in $(seq 1 45); do
      if curl -fsS -m 3 "$LOCAL_URL/healthz" >/dev/null 2>&1; then
        echo "    healthy after ${i}s"; break
      fi
      [ "$i" = 45 ] && die "service did not become healthy within 45s"
      sleep 1
    done
  else
    echo "    [dry-run] systemctl restart $SERVICE"
  fi
fi

[ "$DO_UPLOAD" = 1 ] && purge_cache

# --- verify ----------------------------------------------------------------

say "Verifying"
if [ "$DRY_RUN" = 0 ]; then
  fail=0
  for path in / /pricing /features /docs /app /healthz; do
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "${PUBLIC_URL}${path}" || echo 000)
    printf '    %-10s %s\n' "$path" "$code"
    [ "$code" = 200 ] || fail=1
  done

  ctype=$(curl -s -o /dev/null -w '%{content_type}' -m 20 \
    "${PUBLIC_URL}/static/art/hero-1344.webp" || true)
  printf '    %-10s %s\n' "art" "${ctype:-missing}"
  case "$ctype" in image/webp*) ;; *) warn "art is not being served as image/webp"; fail=1 ;; esac

  [ "$fail" = 0 ] || die "verification failed"
fi

say "Deployed"
echo "    site    ${PUBLIC_URL}"
echo "    assets  https://${R2_PUBLIC_HOST}"
echo "    zone    ${CF_ZONE_ID}"
