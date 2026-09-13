#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if command -v uv >/dev/null 2>&1; then
  wildcat_uv="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
  wildcat_uv="$HOME/.local/bin/uv"
else
  print 'WildCat-3 needs uv installed once: https://docs.astral.sh/uv/getting-started/installation/'
  read '?Press Return to close.'
  exit 1
fi
print 'Preparing WildCat-3. Your browser will open when it is ready…'
if [[ -f data/web-session.json && -x .venv/bin/python ]]; then
  .venv/bin/python -m wildcat3.cli web && exit 0
fi
if ! "$wildcat_uv" sync --frozen --no-editable --reinstall-package wildcat3; then
  read '?Setup failed. Press Return to close.'
  exit 1
fi
"$wildcat_uv" run --no-sync wildcat3 web || { read '?Could not open the panel. Press Return to close.'; exit 1; }
