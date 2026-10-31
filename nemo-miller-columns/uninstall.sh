#!/bin/bash
#
# Nemo Miller Columns – uninstallation
#
set -e
APP_DIR="$HOME/.local/share/nemo-miller-columns"
EXTENSION_DIR="$HOME/.local/share/nemo-python/extensions"
DESKTOP_DIR="$HOME/.local/share/applications"
CONFIG_DIR="$HOME/.config/nemo-miller-columns"

read -p "Uninstall Nemo Miller Columns? [y/N] " -n 1 -r; echo
[[ $REPLY =~ ^[YyJj]$ ]] || { echo "Cancelled."; exit 0; }

rm -rf "$APP_DIR"
rm -f "$EXTENSION_DIR/nemo-miller-columns-extension.py"
rm -f "$DESKTOP_DIR/nemo-miller-columns.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/nemo-miller-columns.svg"
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

read -p "Also remove settings (start folder)? [y/N] " -n 1 -r; echo
[[ $REPLY =~ ^[YyJj]$ ]] && rm -rf "$CONFIG_DIR"

nemo -q 2>/dev/null || true
echo "Uninstalled. If you pinned it to the panel, remove that icon manually."
