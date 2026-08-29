#!/usr/bin/env bash
# Graphiti Lens launcher. Picks the first Python that already has the neo4j
# driver, so there is nothing to install and nothing to keep in sync.
set -euo pipefail
cd "$(dirname "$0")"

CANDIDATES=(
  "./.venv/bin/python3"
  "$HOME/dev/graphiti/mcp_server/.venv/bin/python3"
)

for py in "${CANDIDATES[@]}"; do
  if [ -x "$py" ] && "$py" -c "import neo4j" >/dev/null 2>&1; then
    exec "$py" server.py "$@"
  fi
done

if command -v uv >/dev/null 2>&1; then
  exec uv run --with 'neo4j>=5,<6' --python 3.11 python server.py "$@"
fi
if [ -x "$HOME/.local/bin/uv" ]; then
  exec "$HOME/.local/bin/uv" run --with 'neo4j>=5,<6' --python 3.11 python server.py "$@"
fi

echo "No Python with the neo4j driver found, and uv is not installed." >&2
echo "Fix: python3 -m venv .venv && ./.venv/bin/pip install 'neo4j>=5,<6'" >&2
exit 1
