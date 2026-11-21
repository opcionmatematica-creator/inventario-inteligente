#!/usr/bin/env bash
# Construye el instalador de Windows desde Linux (o macOS) con Docker:
#   bash instalar/windows/construir_instalador.sh
# Resultado: dist/Inventario-Inteligente-<versión>-instalador-windows.exe (+ .sha256)
#
# Incluye Python embebido oficial para Windows (el cliente no instala nada más) y tzdata
# (Windows no trae la base de zonas horarias). Ambas descargas se verifican con SHA-256.
# Requiere: curl, unzip, sha256sum, uv (para Pillow al dibujar el icono) y Docker (NSIS).
set -euo pipefail

PY_VERSION="3.14.10"
PY_SHA256="90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"   # api python.org
TZ_VERSION="2026.6"
TZ_URL="https://files.pythonhosted.org/packages/0a/66/9a06c2227fedfd4919f0eaaa8b94377d9730882a7e4f95823c9a331ab5e1/tzdata-${TZ_VERSION}-py2.py3-none-any.whl"
TZ_SHA256="0a10eec6e13ef16b627fabed9a5f2ba61a62b1c67f327305f4e7e412a70b4aec"   # PyPI
NSIS_IMAGE="debian:stable-slim"

RAIZ_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(python3 -c "import re;print(re.search(r'__version__ = \"(.+)\"', open('$RAIZ_REPO/inventario/__init__.py').read()).group(1))")"
TRABAJO="$RAIZ_REPO/build/windows"
CACHE="$TRABAJO/descargas"
ROOT="$TRABAJO/raiz"
SALIDA="$RAIZ_REPO/dist/Inventario-Inteligente-${VERSION}-instalador-windows.exe"

descargar() {  # url archivo sha256
  local url="$1" archivo="$2" esperado="$3"
  if [ ! -f "$archivo" ] || ! echo "$esperado  $archivo" | sha256sum -c --quiet - 2>/dev/null; then
    echo "Descargando $(basename "$archivo")…"
    curl -fsSL --retry 3 -o "$archivo.tmp" "$url"
    mv "$archivo.tmp" "$archivo"
  fi
  echo "$esperado  $archivo" | sha256sum -c --quiet - || { echo "SHA-256 incorrecto: $archivo" >&2; exit 1; }
}

a_texto_windows() {  # Markdown/UTF-8 → .txt UTF-8 con BOM y CRLF (se abre bien en el Bloc de notas)
  python3 - "$1" "$2" <<'EOF'
import sys
texto = open(sys.argv[1], encoding="utf-8").read()
open(sys.argv[2], "w", encoding="utf-8-sig", newline="\r\n").write(texto)
EOF
}

mkdir -p "$CACHE" "$(dirname "$SALIDA")"
rm -rf "$ROOT"
mkdir -p "$ROOT/python" "$ROOT/app"

descargar "https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-embed-amd64.zip" \
  "$CACHE/python-${PY_VERSION}-embed-amd64.zip" "$PY_SHA256"
descargar "$TZ_URL" "$CACHE/tzdata-${TZ_VERSION}-py2.py3-none-any.whl" "$TZ_SHA256"

# Python embebido + ruta a la app (el archivo ._pth fija sys.path; las rutas son relativas a él).
unzip -q "$CACHE/python-${PY_VERSION}-embed-amd64.zip" -d "$ROOT/python"
PTH="$(ls "$ROOT"/python/python3*._pth)"
printf '%s\r\n' "$(basename "$PTH" ._pth).zip" "." "..\\app" > "$PTH"

# La aplicación (sin cachés) y tzdata.
(cd "$RAIZ_REPO" && find inventario -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 \
  | xargs -0 -I{} install -D -m 644 {} "$ROOT/app/{}")
unzip -q "$CACHE/tzdata-${TZ_VERSION}-py2.py3-none-any.whl" 'tzdata/*' -d "$ROOT/app"

# Icono y documentos.
uvx -q --with pillow python "$RAIZ_REPO/instalar/windows/icono.py" "$ROOT/inventario.ico"
rm -f "$ROOT/inventario.png"
a_texto_windows "$RAIZ_REPO/docs/manual-usuario.md" "$ROOT/Manual de usuario.txt"
a_texto_windows "$RAIZ_REPO/inventario/legal/CONTRATO-LICENCIA.md" "$ROOT/Contrato de licencia.txt"
a_texto_windows "$RAIZ_REPO/AVISOS-TERCEROS.md" "$ROOT/Avisos de terceros.txt"
a_texto_windows "$RAIZ_REPO/LICENSE.md" "$ROOT/Licencia PolyForm Noncommercial.txt"
cp "$ROOT/python/LICENSE.txt" "$ROOT/Licencia de Python.txt"
python3 - "$RAIZ_REPO" "$ROOT/Licencia.txt" <<'EOF'
import sys, pathlib
raiz = pathlib.Path(sys.argv[1])
resumen = (raiz / "LICENCIA-COMERCIAL.md").read_text(encoding="utf-8")
polyform = (raiz / "LICENSE.md").read_text(encoding="utf-8")
texto = resumen + "\n\n---\n\nLICENCIA PÚBLICA (uso no comercial)\n\n" + polyform + (
    "\n\n---\n\nEste instalador incluye Python (licencia PSF) y tzdata (Apache-2.0); "
    "ver los archivos de licencia en la carpeta del programa.\n")
# La página de licencia de NSIS Unicode lee UTF-16 LE con BOM.
pathlib.Path(sys.argv[2]).write_text(texto.replace("\n", "\r\n"), encoding="utf-16")
EOF
printf 'tzdata %s (Apache-2.0): https://pypi.org/project/tzdata/\r\nPython %s embebido (PSF): ver "Licencia de Python.txt"\r\n' \
  "$TZ_VERSION" "$PY_VERSION" >> "$ROOT/Avisos de terceros.txt"

# Compilar con NSIS dentro de un contenedor (no hace falta Windows).
cp "$RAIZ_REPO/instalar/windows/inventario.nsi" "$TRABAJO/inventario.nsi"
docker run --rm -v "$TRABAJO:/trabajo" -v "$(dirname "$SALIDA"):/salida" "$NSIS_IMAGE" bash -c "
  set -e
  apt-get update -qq >/dev/null && apt-get install -y -qq nsis >/dev/null
  makensis -V2 -DVERSION=$VERSION -DVERSION4=$VERSION.0 -DROOT=/trabajo/raiz \
    -DOUT=/salida/$(basename "$SALIDA") /trabajo/inventario.nsi
  chown $(id -u):$(id -g) /salida/$(basename "$SALIDA")"

(cd "$(dirname "$SALIDA")" && sha256sum "$(basename "$SALIDA")" > "$(basename "$SALIDA").sha256")
echo "Listo: $SALIDA ($(du -h "$SALIDA" | cut -f1))"
cat "$SALIDA.sha256"
