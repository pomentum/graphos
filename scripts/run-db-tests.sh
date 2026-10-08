#!/usr/bin/env bash
# Run graphos's DB-gated tests (the ones marked `@requires_db`) against a
# freshly-booted docker-compose stack.
#
# Recipe:
#   1. Bring up docker-compose.graphos.yml (graphos-db + graphos-api).
#   2. Wait for graphos-db to report healthy.
#   3. Run `pytest -m db` inside a throwaway container on the
#      `graphos-internal` network so it can reach `graphos-db` by hostname.
#   4. Tear the stack down on exit (preserves the data volume so re-runs
#      pick up faster; pass `--down-volumes` to wipe it).
#
# Usage:
#   scripts/run-db-tests.sh                  → run all @db tests
#   scripts/run-db-tests.sh tests/test_search.py::test_x   → one test
#   scripts/run-db-tests.sh --down-volumes   → also wipe graphos_data on exit

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_DIR="$ROOT/services/graphos"
COMPOSE_FILE="$ROOT/docker-compose.graphos.yml"

DOWN_VOLUMES=""
PYTEST_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --down-volumes) DOWN_VOLUMES="--volumes" ;;
        *)              PYTEST_ARGS+=("$arg") ;;
    esac
done

cleanup() {
    echo
    echo "==> tearing down stack..."
    docker compose -f "$COMPOSE_FILE" down $DOWN_VOLUMES >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "==> ensuring secret files exist..."
bash "$SCRIPT_DIR/init-secrets.sh"

echo "==> bringing up graphos stack..."
docker compose -f "$COMPOSE_FILE" up -d --build

echo "==> waiting for graphos-db to be healthy..."
deadline=$((SECONDS + 60))
while ! docker ps --filter "name=graphos-db" --format "{{.Status}}" | grep -q "healthy"; do
    if (( SECONDS > deadline )); then
        echo "!! graphos-db did not reach healthy state in 60 s"
        docker logs graphos-db 2>&1 | tail -40
        exit 1
    fi
    sleep 2
done

# The API runs as the unprivileged graphos_app role (deploy/db-init/
# 01-app-role.sh).  Wait until it is ready, then register a tenant and write
# a node and an edge through it, so a broken role setup fails here.
echo "==> waiting for graphos-api to be ready..."
deadline=$((SECONDS + 90))
until curl -fsS http://127.0.0.1:8080/readyz >/dev/null 2>&1; do
    if (( SECONDS > deadline )); then
        echo "!! graphos-api did not become ready in 90 s"
        docker logs graphos-api 2>&1 | tail -40
        exit 1
    fi
    sleep 2
done
# Sign in as the bootstrap admin (graphos / graphos on a fresh volume), do
# the forced password change (to a random password kept in this shell only),
# and use the token it returns.
json_field() {  # field — read one field of the JSON on stdin
    python3 -c "import json, sys; print(json.load(sys.stdin)['$1'])"
}
FIRST=graphos
NEXT="ci-$(openssl rand -hex 16)"
FIRST_TOKEN="$(curl -fsS -X POST http://127.0.0.1:8080/v1/auth/login \
    -H "content-type: application/json" \
    -d "{\"user\": \"graphos\", \"password\": \"$FIRST\"}" | json_field access_token)"
TOKEN="$(curl -fsS -X POST http://127.0.0.1:8080/v1/me/password \
    -H "Authorization: Bearer $FIRST_TOKEN" -H "content-type: application/json" \
    -d "{\"current\": \"$FIRST\", \"new\": \"$NEXT\"}" | json_field access_token)"
api() {  # method path json
    curl -fsS -X "$1" "http://127.0.0.1:8080$2"         -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" -d "$3" >/dev/null
}
echo "==> API smoke test as graphos_app..."
api POST /v1/tenants '{"name": "ci_smoke", "vertex_labels": ["Doc"], "edge_labels": ["LINKS_TO"]}'
api POST /v1/ci_smoke/graph/nodes '{"nodes": [{"id": "a", "label": "Doc"}, {"id": "b", "label": "Doc"}]}'
api POST /v1/ci_smoke/graph/edges     '{"edges": [{"src_id": "a", "src_label": "Doc", "dst_id": "b", "dst_label": "Doc", "type": "LINKS_TO"}]}'

# The PW the test container connects with — pulled from the secret file
# so it always matches what graphos-db actually expects.
PW="$(cat "$SERVICE_DIR/deploy/secrets/graphos_pw.txt" | tr -d '\r\n')"
DSN="postgresql://graphos:${PW}@graphos-db:5432/graphos"

echo "==> running pytest -m db inside an ephemeral python:3.11 container..."
# Defaults to all @db tests when no extra args are passed.
if [[ ${#PYTEST_ARGS[@]} -eq 0 ]]; then
    PYTEST_ARGS=(-m db)
fi

# The test container needs:
#   - `graphos-net`      → internet egress (for `pip install`)
#   - `graphos-internal` → reach `graphos-db` (which is internal-only)
#
# Docker run only accepts one --network at start. We launch on
# graphos-net, then attach graphos-internal before the test command
# fires. (MSYS_NO_PATHCONV=1 stops git-bash on Windows from translating
# the container-side `/work` in `-v` to a native Windows path.)
container=$(
    MSYS_NO_PATHCONV=1 docker run -d \
        --network graphos-net \
        -v "$SERVICE_DIR:/work" \
        -e "GRAPHOS_TEST_DSN=$DSN" \
        python:3.11-slim \
        sleep 1800
)
trap '
    docker network disconnect graphos-internal "$container" >/dev/null 2>&1 || true
    docker rm -f "$container" >/dev/null 2>&1 || true
    cleanup
' EXIT INT TERM

docker network connect graphos-internal "$container"

docker exec "$container" sh -c "
    cd /work && \
    pip install -q -e '.[dev]' && \
    pytest -v ${PYTEST_ARGS[*]}
"
