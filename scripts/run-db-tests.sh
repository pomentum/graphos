#!/usr/bin/env bash
# Run graphos's DB-gated tests (the ones marked `@requires_db`) against a
# freshly-booted docker-compose stack.
#
# Recipe:
#   1. Bring up docker-compose.graphos.yml (graphos-db + graphos-api) as a
#      compose project of its own, `graphos-test` (see "Isolation" below).
#   2. Wait for graphos-db to report healthy and graphos-api to be ready.
#   3. Run `pytest -m db` inside a throwaway container on the stack's
#      internal network so it can reach `graphos-db` by hostname.
#   4. Tear the stack down on exit (preserves the data volume so re-runs
#      pick up faster; pass `--down-volumes` to wipe it).
#
# Isolation: the test stack shares nothing with a graphos stack running on
# the same machine. Its containers, volumes and networks are renamed
# (graphos-test-*, graphos_test_*), graphos-api listens on
# 127.0.0.1:$GRAPHOS_TEST_API_PORT (default 18089) and is built as
# graphos-api:test. Before removing volumes the script checks that every
# volume of the project is a graphos_test_* one, and refuses otherwise.
#
# Usage:
#   scripts/run-db-tests.sh                  → run all @db tests
#   scripts/run-db-tests.sh tests/test_search.py::test_x   → one test
#   scripts/run-db-tests.sh --down-volumes   → also wipe the test volumes on exit
#   scripts/run-db-tests.sh --db-image IMAGE → graphos-db on another database image
#                                              (a newer AGE, say; graphos needs 18 / 1.8),
#                                              on a volume of its own

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_DIR="$ROOT/services/graphos"
COMPOSE_FILE="$ROOT/docker-compose.graphos.yml"

PROJECT=graphos-test
DB_CONTAINER=graphos-test-db
API_CONTAINER=graphos-test-api
INTERNAL_NET=graphos-test-internal
EXTERNAL_NET=graphos-test-net
API_PORT="${GRAPHOS_TEST_API_PORT:-18089}"
API_URL="http://127.0.0.1:${API_PORT}"
export GRAPHOS_API_TAG=test     # never replaces the image a running stack uses

DOWN_VOLUMES=""
DB_IMAGE=""
PYTEST_ARGS=()
while (( $# )); do
    case "$1" in
        --down-volumes) DOWN_VOLUMES="--volumes" ;;
        --db-image)     DB_IMAGE="$2"; shift ;;
        *)              PYTEST_ARGS+=("$1") ;;
    esac
    shift
done

# A Python that runs (on Windows `python3` can be the Microsoft Store stub).
PY=""
for candidate in python3 python; do
    if "$candidate" -c "" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [[ -z "$PY" ]]; then
    echo "!! run-db-tests.sh needs python3 (or python) on PATH" >&2
    exit 1
fi

# Every name the compose file fixes is replaced, so nothing of a stack running
# on the same machine (graphos-db, graphos_data, graphos-net, port 8080, ...)
# is touched. The PostgreSQL 16 volume is a new, empty one: the move from
# PostgreSQL 16 finds nothing to move, as on a new install. Another database
# image gets a data volume of its own (another major's data must never land
# on the PostgreSQL 18 one) and no move.
if [[ -n "$DB_IMAGE" ]]; then
    DATA_VOLUME=graphos_test_db
    DB_OVERRIDE="    image: $DB_IMAGE"
    MOVE_OVERRIDE='  graphos-db-export:
    container_name: graphos-test-db-export
    entrypoint: ["true"]
  graphos-db-import:
    container_name: graphos-test-db-import
    entrypoint: ["true"]'
else
    DATA_VOLUME=graphos_test_db_pg18
    DB_OVERRIDE=""
    MOVE_OVERRIDE='  graphos-db-export:
    container_name: graphos-test-db-export
  graphos-db-import:
    container_name: graphos-test-db-import'
fi
OVERRIDE="$(mktemp)"
cat > "$OVERRIDE" <<EOF
services:
  graphos-db:
    container_name: $DB_CONTAINER
$DB_OVERRIDE
$MOVE_OVERRIDE
  graphos-api:
    container_name: $API_CONTAINER
    ports: !override
      - "127.0.0.1:${API_PORT}:8080"
  graphos-ui:
    container_name: graphos-test-ui
volumes:
  graphos_data:
    name: graphos_test_old_pg16
  graphos_data_pg18:
    name: $DATA_VOLUME
  graphos_upgrade:
    name: graphos_test_upgrade
networks:
  graphos-internal:
    name: $INTERNAL_NET
  graphos-net:
    name: $EXTERNAL_NET
EOF
COMPOSE=(docker compose -p "$PROJECT" -f "$COMPOSE_FILE" -f "$OVERRIDE")

# The project's volume names, as compose resolves them; all must be test ones.
"${COMPOSE[@]}" config --format json | "$PY" -c '
import json, sys
names = [v.get("name", "") for v in json.load(sys.stdin).get("volumes", {}).values()]
bad = [n for n in names if not n.startswith("graphos_test_")]
if bad:
    sys.exit("!! refusing to run: the test project would use volumes " + ", ".join(bad))
'

cleanup() {
    echo
    echo "==> tearing down the test stack..."
    if [[ -n "$DOWN_VOLUMES" ]] && ! "${COMPOSE[@]}" config --format json | "$PY" -c '
import json, sys
names = [v.get("name", "") for v in json.load(sys.stdin).get("volumes", {}).values()]
sys.exit(any(not n.startswith("graphos_test_") for n in names))
'; then
        echo "!! not removing volumes: the project holds volumes other than graphos_test_*"
        DOWN_VOLUMES=""
    fi
    "${COMPOSE[@]}" down $DOWN_VOLUMES >/dev/null 2>&1 || true
    rm -f "$OVERRIDE"
}
trap cleanup EXIT INT TERM

if (exec 3<>"/dev/tcp/127.0.0.1/${API_PORT}") 2>/dev/null; then
    echo "!! port ${API_PORT} is in use; set GRAPHOS_TEST_API_PORT to a free one" >&2
    exit 1
fi

echo "==> ensuring secret files exist..."
bash "$SCRIPT_DIR/init-secrets.sh"

echo "==> bringing up the test stack (project $PROJECT)..."
"${COMPOSE[@]}" up -d --build

echo "==> waiting for graphos-db to be healthy..."
deadline=$((SECONDS + 60))
until [[ "$(docker inspect -f '{{.State.Health.Status}}' "$DB_CONTAINER" 2>/dev/null)" == healthy ]]; do
    if (( SECONDS > deadline )); then
        echo "!! graphos-db did not reach healthy state in 60 s"
        docker logs "$DB_CONTAINER" 2>&1 | tail -40
        exit 1
    fi
    sleep 2
done

# The API runs as the unprivileged graphos_app role (deploy/db-init/
# 01-app-role.sh).  Wait until it is ready, then register a tenant and write
# a node and an edge through it, so a broken role setup fails here.
echo "==> waiting for graphos-api to be ready..."
deadline=$((SECONDS + 90))
until curl -fsS "$API_URL/readyz" >/dev/null 2>&1; do
    if (( SECONDS > deadline )); then
        echo "!! graphos-api did not become ready in 90 s"
        docker logs "$API_CONTAINER" 2>&1 | tail -40
        exit 1
    fi
    sleep 2
done
# Sign in as the bootstrap admin (graphos / graphos on a fresh volume), do
# the forced password change (to a random password kept in this shell only),
# and use the token it returns.
json_field() {  # field — read one field of the JSON on stdin
    "$PY" -c "import json, sys; print(json.load(sys.stdin)['$1'])"
}
FIRST=graphos
NEXT="ci-$(openssl rand -hex 16)"
FIRST_TOKEN="$(curl -fsS -X POST "$API_URL/v1/auth/login" \
    -H "content-type: application/json" \
    -d "{\"user\": \"graphos\", \"password\": \"$FIRST\"}" | json_field access_token)"
TOKEN="$(curl -fsS -X POST "$API_URL/v1/me/password" \
    -H "Authorization: Bearer $FIRST_TOKEN" -H "content-type: application/json" \
    -d "{\"current\": \"$FIRST\", \"new\": \"$NEXT\"}" | json_field access_token)"
api() {  # method path json
    curl -fsS -X "$1" "$API_URL$2" \
        -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" -d "$3" >/dev/null
}
echo "==> API smoke test as graphos_app..."
api POST /v1/tenants '{"name": "ci_smoke", "vertex_labels": ["Doc"], "edge_labels": ["LINKS_TO"]}'
api POST /v1/ci_smoke/graph/nodes '{"nodes": [{"id": "a", "label": "Doc"}, {"id": "b", "label": "Doc"}]}'
api POST /v1/ci_smoke/graph/edges     '{"edges": [{"src_id": "a", "src_label": "Doc", "dst_id": "b", "dst_label": "Doc", "type": "LINKS_TO"}]}'

# The PW the test container connects with — pulled from the secret file
# so it always matches what graphos-db actually expects.
PW="$(cat "$SERVICE_DIR/deploy/secrets/graphos_pw.txt" | tr -d '\r\n')"
DSN="postgresql://graphos:${PW}@graphos-db:5432/graphos"
# graphos_app's, for the tests that start graphos as that role (test_db_setup_db.py).
APP_PW="$(cat "$SERVICE_DIR/deploy/secrets/graphos_app_pw.txt" | tr -d '\r\n')"

echo "==> running pytest -m db inside an ephemeral python:3.11 container..."
# Defaults to all @db tests when no extra args are passed.
if [[ ${#PYTEST_ARGS[@]} -eq 0 ]]; then
    PYTEST_ARGS=(-m db)
fi

# The test container needs:
#   - the external network → internet egress (for `pip install`)
#   - the internal network → reach `graphos-db` (which is internal-only)
#
# Docker run only accepts one --network at start. We launch on the
# external one, then attach the internal one before the test command
# fires. (MSYS_NO_PATHCONV=1 stops git-bash on Windows from translating
# the container-side `/work` in `-v` to a native Windows path.)
container=$(
    MSYS_NO_PATHCONV=1 docker run -d \
        --network "$EXTERNAL_NET" \
        -v "$SERVICE_DIR:/work" \
        -e "GRAPHOS_TEST_DSN=$DSN" \
        -e "GRAPHOS_TEST_APP_PASSWORD=$APP_PW" \
        -e "GRAPHOS_TEST_MASTER_KEY_FILE=/work/deploy/secrets/master_key.txt" \
        python:3.11-slim \
        sleep 1800
)
trap '
    docker network disconnect "$INTERNAL_NET" "$container" >/dev/null 2>&1 || true
    docker rm -f "$container" >/dev/null 2>&1 || true
    cleanup
' EXIT INT TERM

docker network connect "$INTERNAL_NET" "$container"

docker exec "$container" sh -c "
    cd /work && \
    pip install -q -e '.[dev,louvain]' && \
    pytest -v ${PYTEST_ARGS[*]}
"
