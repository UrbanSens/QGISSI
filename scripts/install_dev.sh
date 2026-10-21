#!/usr/bin/env bash
# Link this checkout into a QGIS profile's plugin folder for development.
# Usage: scripts/install_dev.sh [profile-name]   (default: "default"). Restart QGIS afterwards
# and enable "QGISSI" in Plugins > Manage and Install Plugins.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${1:-default}"
case "$(uname)" in
  Darwin) BASE="$HOME/Library/Application Support/QGIS/QGIS3/profiles" ;;
  Linux)  BASE="${XDG_DATA_HOME:-$HOME/.local/share}/QGIS/QGIS3/profiles" ;;
  *) echo "On Windows, copy or link the qgissi folder into %APPDATA%\\QGIS\\QGIS3\\profiles\\$PROFILE\\python\\plugins" >&2; exit 1 ;;
esac
TARGET="$BASE/$PROFILE/python/plugins"
mkdir -p "$TARGET"
if [[ -e "$TARGET/qgissi" && ! -L "$TARGET/qgissi" ]]; then
  echo "$TARGET/qgissi exists and is not a link; remove it first." >&2; exit 1
fi
ln -sfn "$REPO/qgissi" "$TARGET/qgissi"
echo "Linked $REPO/qgissi -> $TARGET/qgissi"
