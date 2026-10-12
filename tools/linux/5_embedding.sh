#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/env.sh"
exec "$G4W_PY" -u "$G4W_HOME/G4W/memory/vector/install_embedding.py" "$@"
