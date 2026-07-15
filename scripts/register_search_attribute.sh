#!/usr/bin/env bash
# Register the custom search attribute the approval inbox filters on to list the
# workflows parked at the approval gate. One-time per Temporal namespace; safe to
# re-run (a create that fails because it already exists is treated as success —
# note this also tolerates a server being down, so confirm the attribute if the
# inbox later shows nothing parked).
#
#   ./scripts/register_search_attribute.sh
#
# Requires the Temporal CLI (`temporal`) and a running server (docker compose up -d).
set -euo pipefail

ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}"
NAMESPACE="${TEMPORAL_NAMESPACE:-default}"

echo "Registering search attribute 'farthing_state' (Keyword) on ${ADDRESS} / ${NAMESPACE} ..."
temporal operator search-attribute create \
  --address "${ADDRESS}" \
  --namespace "${NAMESPACE}" \
  --name farthing_state \
  --type Keyword \
  || echo "(already registered or non-fatal — continuing)"

echo "Done. The inbox can now list workflows where farthing_state = 'awaiting_approval'."
