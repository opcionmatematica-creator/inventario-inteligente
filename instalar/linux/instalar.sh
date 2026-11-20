#!/usr/bin/env bash
# Instala el acceso «Inventario Inteligente» en el menú de aplicaciones (Linux).
# Uso: bash instalar/linux/instalar.sh   (desde cualquier carpeta)
set -euo pipefail
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then echo "Hace falta Python 3.11 o posterior (sudo apt install python3)." >&2; exit 1; fi
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Hace falta Python 3.11 o posterior." >&2; exit 1; }
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONOS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
mkdir -p "$APPS" "$ICONOS"
cp "$RAIZ/inventario/web/icon.svg" "$ICONOS/inventario-inteligente.svg"
cat > "$APPS/inventario-inteligente.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Inventario Inteligente
Comment=Caja e inventario sin internet
Exec=sh -c 'cd "$RAIZ" && exec "$PY" -m inventario.app'
Icon=inventario-inteligente
Terminal=false
Categories=Office;Finance;
StartupWMClass=inventario-inteligente
DESKTOP
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true
echo "Listo: busque «Inventario Inteligente» en el menú de aplicaciones."
echo "Los datos se guardan en ${XDG_DATA_HOME:-$HOME/.local/share}/inventario-inteligente"
