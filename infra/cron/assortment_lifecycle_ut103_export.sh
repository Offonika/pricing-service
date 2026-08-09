#!/usr/bin/env bash
set -euo pipefail

# Compatibility tombstone for stale installations. Lifecycle classification is
# stored only in pricing-service and must never produce UT 10.3 property files.
echo "Lifecycle property export to UT 10.3 is retired; no file was created." >&2
exit 64
