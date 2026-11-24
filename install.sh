#!/usr/bin/env bash
# Install from a clone: ./install.sh   (installs and switches routing on; other options: see README.md)
# All logic lives in scripts/install.py.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null 2>&1 || { echo "install.sh: python3 (3.10+) is required" >&2; exit 1; }
if [[ $# -eq 0 ]]; then set -- --enable --friendly; fi
exec python3 -I "${here}/scripts/install.py" "$@"
