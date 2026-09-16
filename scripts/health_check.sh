#!/usr/bin/env bash
set -uo pipefail

failures=0

pass() {
    printf 'PASS: %s\n' "$1"
}

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    failures=$((failures + 1))
}

check_service() {
    local service="$1"
    if systemctl is-active --quiet "${service}"; then
        pass "service ${service} is active"
    else
        fail "service ${service} is not active"
    fi
}

check_health_url() {
    local label="$1"
    local url="$2"
    local response

    if ! response="$(curl --fail --silent --show-error --max-time 5 "${url}")"; then
        fail "${label} did not return HTTP success"
        return
    fi

    if grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"${response}"; then
        pass "${label} returned status=ok"
    else
        fail "${label} returned an unexpected response"
    fi
}

check_service tracker-tcp
check_service tracker-api
check_service nginx

if ss -ltnH 'sport = :8686' | grep -q .; then
    pass "TCP port 8686 is listening"
else
    fail "TCP port 8686 is not listening"
fi

if ss -ltnH 'sport = :8000' | grep -q '127.0.0.1:8000'; then
    pass "API port 8000 is listening on localhost"
else
    fail "API port 8000 is not listening on 127.0.0.1"
fi

check_health_url "direct API health check" "http://127.0.0.1:8000/api/health"
check_health_url "Nginx API health check" "http://127.0.0.1/api/health"

if [[ ${failures} -ne 0 ]]; then
    printf '%s health check(s) failed.\n' "${failures}" >&2
    exit 1
fi

echo "All health checks passed."
