#!/usr/bin/env bash
# init-secrets.sh — create graphos local secrets on first run.
#
# Writes into services/graphos/deploy/secrets/ (gitignored):
#   graphos_pw.txt       random password for the database superuser (init only)
#   graphos_app_pw.txt   random password for graphos_app, the role graphos-api uses
#
# Idempotent: never overwrites an existing file. Safe to run before every
# `docker compose up`.
#
# Files are written 0644 on purpose: they are bind-mounted into containers
# whose processes run as other uids (postgres, graphos), which could not
# read a 0600 file owned by your user. Keep the directory itself private.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
secrets_dir="$(cd "${here}/../services/graphos/deploy/secrets" && pwd)"

gen_secret() {
  # 32 random alphanumerics. Prefer openssl, fall back to /dev/urandom.
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-32
  else
    LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32
  fi
}

ensure_password() {  # file name
  local target="${secrets_dir}/$1"
  if [[ -s "${target}" ]]; then
    echo "ok: $1 already present"
    return 0
  fi
  # No trailing newline: postgres reads the file as-is.
  printf '%s' "$(gen_secret)" > "${target}"
  chmod 644 "${target}" 2>/dev/null || true
  echo "created: $1"
}

ensure_password graphos_pw.txt
ensure_password graphos_app_pw.txt
# graphos's own first admin is user "graphos", password "graphos", to be
# changed at the first sign-in (or set GRAPHOS_ADMIN_PASSWORD_FILE).
