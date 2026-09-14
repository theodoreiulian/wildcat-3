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
# The standalone window needs Electron once. Finder launches this without an interactive
# shell, so look where Node usually lives before giving up on the window.
if [[ ! -e desktop/node_modules/electron/path.txt ]]; then
  if ! command -v npm >/dev/null 2>&1; then
    for wildcat_bin in /opt/homebrew/bin /usr/local/bin "$HOME/.nvm/versions/node"/*/bin(N/On); do
      if [[ -x "$wildcat_bin/npm" ]]; then
        PATH="$wildcat_bin:$PATH"
        break
      fi
    done
  fi
  if command -v npm >/dev/null 2>&1; then
    print 'Preparing the WildCat-3 window. The first run downloads Electron…'
    npm ci --prefix desktop --no-audit --no-fund \
      || npm install --prefix desktop --no-audit --no-fund \
      || print 'Could not install the window; WildCat-3 will open in your browser instead.'
  else
    print 'Node.js is not installed, so WildCat-3 will open in your browser.'
    print 'Install Node.js from https://nodejs.org to get the standalone window.'
  fi
fi
print 'Preparing WildCat-3. Your window will open when it is ready…'
if [[ -f data/web-session.json && -x .venv/bin/python ]]; then
  .venv/bin/python -m wildcat3.cli web && exit 0
fi
if ! "$wildcat_uv" sync --frozen --no-editable --reinstall-package wildcat3; then
  read '?Setup failed. Press Return to close.'
  exit 1
fi
"$wildcat_uv" run --no-sync wildcat3 web || { read '?Could not open the panel. Press Return to close.'; exit 1; }
