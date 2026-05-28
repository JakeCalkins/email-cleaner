#!/usr/bin/env bash
set -euo pipefail

# Installs mailzero as a shell command.
# Preferred path: pipx (isolated venv, best on modern macOS Python installs).
# Fallback: user-site pip install with --break-system-packages.
# No code signing/Xcode required.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 not found in PATH." >&2
  exit 1
fi

if command -v pipx >/dev/null 2>&1; then
  pipx install --force "${REPO_ROOT}"
  BIN_DIR="${HOME}/.local/bin"
else
  python3 -m pip install --user --break-system-packages --editable "${REPO_ROOT}"
  USER_BASE="$(python3 - <<'PY'
import site
print(site.USER_BASE)
PY
)"
  BIN_DIR="${USER_BASE}/bin"
fi

if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
  echo
  echo "mailzero was installed, but ${BIN_DIR} is not on PATH."
  echo "Add this line to ~/.zshrc and open a new terminal:"
  echo "  export PATH=\"${BIN_DIR}:\$PATH\""
  echo
fi

echo "Installed. You can run: mailzero --help"
