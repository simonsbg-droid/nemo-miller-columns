#!/bin/bash
#
# Nemo Miller Columns – installation
# Tested on Linux Mint 22 (Cinnamon, Nemo 6.x)
#
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

APP_DIR="$HOME/.local/share/nemo-miller-columns"
EXTENSION_DIR="$HOME/.local/share/nemo-python/extensions"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
CONFIG_DIR="$HOME/.config/nemo-miller-columns"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${GREEN}Nemo Miller Columns – installation${NC}\n"

if ! command -v nemo &> /dev/null; then
    echo -e "${RED}Nemo not found. This extension needs the Nemo file manager.${NC}"
    exit 1
fi

echo -e "${YELLOW}[1/5] Checking dependencies …${NC}"
if ! python3 -c "import gi; gi.require_version('Gtk', '3.0')" 2>/dev/null \
   || ! python3 -c "import gi; gi.require_version('Nemo', '3.0')" 2>/dev/null; then
    sudo apt update
    sudo apt install -y python3 python3-gi gir1.2-gtk-3.0 nemo-python
fi

echo -e "${YELLOW}[2/5] Installing application …${NC}"
mkdir -p "$APP_DIR" "$EXTENSION_DIR" "$DESKTOP_DIR" "$CONFIG_DIR"
if [ -f "$APP_DIR/nemo_miller_columns.py" ]; then
    cp "$APP_DIR/nemo_miller_columns.py" "$APP_DIR/nemo_miller_columns.py.previous"
fi
cp "$SCRIPT_DIR/nemo_miller_columns.py" "$APP_DIR/"
chmod +x "$APP_DIR/nemo_miller_columns.py"

echo -e "${YELLOW}[3/5] Installing Nemo extension …${NC}"
cp "$SCRIPT_DIR/nemo-miller-columns-extension.py" "$EXTENSION_DIR/"
mkdir -p "$ICON_DIR"
cp "$SCRIPT_DIR/nemo-miller-columns.svg" "$ICON_DIR/"

echo -e "${YELLOW}[4/5] Creating launcher …${NC}"
cat > "$DESKTOP_DIR/nemo-miller-columns.desktop" << DESKTOP
[Desktop Entry]
Name=Nemo Miller Columns
Name[de]=Spaltenansicht
Comment=Browse folders in columns, like the macOS Finder
Comment[de]=Ordner in Spalten durchsuchen, wie im macOS Finder
Exec=python3 $APP_DIR/nemo_miller_columns.py %U
Icon=nemo-miller-columns
Terminal=false
Type=Application
Categories=Utility;FileTools;FileManager;
MimeType=inode/directory;
Keywords=file;manager;miller;columns;finder;Dateien;Spalten;
StartupWMClass=nemo-miller-columns
Actions=new-window;

[Desktop Action new-window]
Name=New Window
Name[de]=Neues Fenster
Exec=python3 $APP_DIR/nemo_miller_columns.py
DESKTOP
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo -e "${YELLOW}[5/5] Start folder …${NC}"
if [ ! -f "$CONFIG_DIR/startordner" ]; then
    read -r -p "Folder to open on start [${HOME}]: " START
    START="${START:-$HOME}"
    START="${START/#\~/$HOME}"
    if [ -d "$START" ]; then
        echo "$START" > "$CONFIG_DIR/startordner"
        echo -e "${GREEN}Start folder: $START${NC}"
    else
        echo -e "${RED}'$START' is not a folder – using your home folder.${NC}"
    fi
else
    echo "Keeping existing start folder: $(cat "$CONFIG_DIR/startordner")"
fi

nemo -q 2>/dev/null || true

echo -e "\n${GREEN}Done!${NC}"
echo "Open it from the menu (\"Nemo Miller Columns\"), or in Nemo via right-click → \"Open in Miller Columns\"."
echo "Change the start folder and other options later via the menu button (☰) → Settings."
