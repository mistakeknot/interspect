#!/usr/bin/env bash
# Authoritative SessionEnd entrypoint for routing calibration.
# Exit 0: artifact updated; 2: valid no-op (no scores); 1: hard failure.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/../hooks/lib-interspect.sh"

interspect_write_routing_calibration_main() {
    command -v jq >/dev/null 2>&1 || return 1
    command -v sqlite3 >/dev/null 2>&1 || return 1
    _interspect_ensure_db >/dev/null 2>&1 || return 1

    local status
    _interspect_write_routing_calibration
    status=$?
    case "$status" in
        0) return 0 ;;
        2) return 2 ;;
        *) return 1 ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    interspect_write_routing_calibration_main
    exit $?
fi
