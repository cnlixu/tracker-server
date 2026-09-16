#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_ROOT="/opt/tracker"
readonly VENV_DIR="${PROJECT_ROOT}/.venv"
readonly SERVICES=(tracker-tcp tracker-api)

run_privileged() {
    if [[ ${EUID} -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

show_service_diagnostics() {
    local service
    echo "Service diagnostics:" >&2
    for service in "${SERVICES[@]}"; do
        echo "--- ${service}: status ---" >&2
        run_privileged systemctl --no-pager --full status "${service}" >&2 || true
        echo "--- ${service}: recent journal ---" >&2
        run_privileged journalctl --no-pager -u "${service}" -n 50 >&2 || true
    done
}

deployment_error() {
    local exit_status=$?
    echo "ERROR: deployment stopped with exit status ${exit_status}" >&2
    show_service_diagnostics
    exit "${exit_status}"
}
trap deployment_error ERR

if [[ "$(pwd -P)" != "${PROJECT_ROOT}" ]]; then
    echo "ERROR: run this script from ${PROJECT_ROOT}" >&2
    exit 1
fi

if [[ ! -d .git ]]; then
    echo "ERROR: ${PROJECT_ROOT} is not a Git working tree" >&2
    exit 1
fi

if [[ ! -f .env ]]; then
    echo "ERROR: ${PROJECT_ROOT}/.env is missing" >&2
    exit 1
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "ERROR: Python venv not found at ${VENV_DIR}" >&2
    exit 1
fi

if [[ ${EUID} -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: sudo is required to restart system services" >&2
    exit 1
fi

echo "Current Git commit: $(git rev-parse HEAD)"
git pull --ff-only
echo "Deploying Git commit: $(git rev-parse HEAD)"

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install -r backend/requirements.txt

# Reserved for a future versioned database migration command.
# Run migrations here before restarting either application service.

restart_failed=0
for service in "${SERVICES[@]}"; do
    echo "Restarting ${service}..."
    if ! run_privileged systemctl restart "${service}"; then
        echo "ERROR: failed to restart ${service}" >&2
        restart_failed=1
    fi
done

for service in "${SERVICES[@]}"; do
    if run_privileged systemctl is-active --quiet "${service}"; then
        echo "ACTIVE: ${service}"
    else
        echo "ERROR: ${service} is not active" >&2
        restart_failed=1
    fi
done

if [[ ${restart_failed} -ne 0 ]]; then
    show_service_diagnostics
    exit 1
fi

echo "Deployment completed successfully."
