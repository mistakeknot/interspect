#!/usr/bin/env bash
# Run the weekly review-loop job steps: freeze, sample, aggregate.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$here" exec python3 -m review_loop "$@"
