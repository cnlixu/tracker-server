#!/bin/bash

set -euo pipefail

PROJECT_DIR="/opt/tracker"
VENV_DIR="$PROJECT_DIR/.venv"
REQUIREMENTS="$PROJECT_DIR/backend/requirements.txt"
SCHEMA_FILE="$PROJECT_DIR/backend/sql/schema.sql"

TCP_SERVICE="tracker-tcp"
API_SERVICE="tracker-api"

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-tracker}"
DB_USER="${DB_USER:-tracker}"

echo "========================================"
echo " Tracker deployment"
echo "========================================"

cd "$PROJECT_DIR"

echo
echo "[1/8] Check working tree"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: Server working tree has local changes."
    echo "Please inspect before deployment:"
    git status --short
    exit 1
fi

echo "Branch:"
git branch --show-current

echo "Current commit:"
git rev-parse --short HEAD


echo
echo "[2/8] Pull latest code"

git pull --ff-only

echo "New commit:"
git rev-parse --short HEAD


echo
echo "[3/8] Activate Python virtualenv"

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "ERROR: Python virtualenv not found:"
    echo "$VENV_DIR"
    exit 1
fi

source "$VENV_DIR/bin/activate"


echo
echo "[4/8] Update Python dependencies"

python -m pip install -r "$REQUIREMENTS"


echo
echo "[5/8] Apply database schema"

if [ ! -f "$SCHEMA_FILE" ]; then
    echo "ERROR: Schema file not found:"
    echo "$SCHEMA_FILE"
    exit 1
fi

echo "Database: $DB_NAME"
echo "User:     $DB_USER"

psql \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 \
    -f "$SCHEMA_FILE"


echo
echo "[6/8] Restart services"

sudo systemctl restart "$TCP_SERVICE"
sudo systemctl restart "$API_SERVICE"

sleep 2


echo
echo "[7/8] Check services"

if ! sudo systemctl is-active --quiet "$TCP_SERVICE"; then
    echo "ERROR: $TCP_SERVICE failed"
    sudo systemctl status "$TCP_SERVICE" --no-pager
    sudo journalctl -u "$TCP_SERVICE" -n 50 --no-pager
    exit 1
fi

echo "$TCP_SERVICE: OK"

if ! sudo systemctl is-active --quiet "$API_SERVICE"; then
    echo "ERROR: $API_SERVICE failed"
    sudo systemctl status "$API_SERVICE" --no-pager
    sudo journalctl -u "$API_SERVICE" -n 50 --no-pager
    exit 1
fi

echo "$API_SERVICE: OK"


echo
echo "[8/8] Health checks"

echo "Check TCP port 8686..."

if ss -lnt | grep -q ':8686 '; then
    echo "TCP 8686: OK"
else
    echo "ERROR: TCP port 8686 is not listening"
    sudo journalctl -u "$TCP_SERVICE" -n 50 --no-pager
    exit 1
fi


echo
echo "Check API..."

if curl \
    --fail \
    --silent \
    --show-error \
    --max-time 5 \
    http://127.0.0.1:8000/api/health >/dev/null; then

    echo "API health: OK"

else

    echo "ERROR: API health check failed"

    sudo journalctl -u "$API_SERVICE" -n 50 --no-pager

    exit 1
fi


echo
echo "========================================"
echo " Deployment successful"
echo " Commit: $(git rev-parse --short HEAD)"
echo "========================================"