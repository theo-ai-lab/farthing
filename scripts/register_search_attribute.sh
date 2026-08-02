#!/usr/bin/env bash
# Register the custom search attribute the approval inbox filters on to list the
# workflows parked at the approval gate. One-time per Temporal namespace.
#
#   ./scripts/register_search_attribute.sh
#
# Re-running is safe: Temporal's `search-attribute create` is idempotent and
# reports "Search attributes have been added" for an attribute that already
# exists. Because of that, this script does NOT swallow errors — a failure here
# is a real failure, never an "already registered" false alarm.
#
# It then VERIFIES the attribute is present before claiming success. An earlier
# version printed "Done. The inbox can now list workflows ..." unconditionally,
# including after `connection refused`, and exited 0. The cost of that lie is
# paid much later and somewhere else: a workflow that upserts an unregistered
# attribute fails its workflow task with BAD_SEARCH_ATTRIBUTES, surfacing to the
# caller only as "Unable to query workflow due to Workflow Task in failed state",
# and an inbox pointed at that namespace renders an empty list that looks exactly
# like "no refunds are waiting".
#
# Requires the Temporal CLI (`temporal`) and a running server (docker compose up -d).
set -euo pipefail

ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}"
NAMESPACE="${TEMPORAL_NAMESPACE:-default}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Single source of truth for the name: farthing.workflows upserts this exact
# attribute, so reading it here makes drift between the two impossible.
ATTR="$(PYTHONPATH="$ROOT/src" python3 -c 'from farthing.preconditions import REQUIRED_SEARCH_ATTRIBUTE; print(REQUIRED_SEARCH_ATTRIBUTE)')"

echo "Registering search attribute '${ATTR}' (Keyword) on ${ADDRESS} / ${NAMESPACE} ..."
temporal operator search-attribute create \
  --address "${ADDRESS}" \
  --namespace "${NAMESPACE}" \
  --name "${ATTR}" \
  --type Keyword

# Verify, do not assume. `create` exiting 0 is not proof the namespace now
# carries the attribute; listing it is.
temporal operator search-attribute list \
  --address "${ADDRESS}" --namespace "${NAMESPACE}" -o json \
| PYTHONPATH="$ROOT/src" python3 -c '
import json, sys
from farthing.preconditions import missing_search_attribute
custom = json.load(sys.stdin).get("customAttributes", {})
problem = missing_search_attribute(custom)
if problem:
    sys.exit(f"ERROR: registration reported success but verification failed.\n{problem}")
'

echo "Verified. The inbox can now list workflows where ${ATTR} = 'awaiting_approval'."
