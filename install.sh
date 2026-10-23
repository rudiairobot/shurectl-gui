#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$HOME/.local/bin"
mkdir -p "$HOME/.local/share/applications"

chmod +x "$SCRIPT_DIR/shurectl_gui.py"
ln -sf "$SCRIPT_DIR/shurectl_gui.py" "$HOME/.local/bin/shurectl-gui"

sed "s|Exec=.*|Exec=$HOME/.local/bin/shurectl-gui|g" \
    "$SCRIPT_DIR/shurectl-gui.desktop" > "$HOME/.local/share/applications/shurectl-gui.desktop"

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo "Installed shurectl-gui to ~/.local/bin/shurectl-gui"
echo "Desktop launcher installed to ~/.local/share/applications/shurectl-gui.desktop"
