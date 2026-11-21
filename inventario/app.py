"""Aplicación de escritorio: servidor local + ventana de aplicación.

    python3 -m inventario.app

Si el servidor ya está en marcha (otra ventana abierta), solo abre una ventana
nueva. La ventana es un navegador Chromium (Edge, Chrome, Brave o Chromium) en
modo aplicación con un perfil propio; si no hay ninguno, se usa el navegador por
defecto. Al cerrar la ventana que arrancó el servidor, el servidor se detiene.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from .server import DEFAULT_PORT, create_server, data_dir

CANDIDATES = {
    "win32": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
              r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
              r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
              r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"],
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
               "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
    "linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
              "microsoft-edge", "brave-browser", "brave"],
}


def find_browser() -> str | None:
    override = os.environ.get("INVENTARIO_NAVEGADOR")
    if override:
        return override
    for candidate in CANDIDATES.get(sys.platform, CANDIDATES["linux"]):
        path = os.path.expandvars(candidate)
        if os.path.isabs(path):
            if os.path.isfile(path):
                return path
        elif shutil.which(path):
            return shutil.which(path)
    return None


def running(port: int) -> bool:
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/estado",
                                         headers={"X-Inventario": "1"})
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return "version" in json.loads(response.read())
    except (OSError, ValueError):
        return False


def open_window(url: str, profile: Path) -> subprocess.Popen | None:
    browser = find_browser()
    if browser is None:
        webbrowser.open(url)
        return None
    profile.mkdir(parents=True, exist_ok=True)
    args = [browser, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--window-size=1280,820"]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


LOG_MAX_BYTES = 1_000_000


def redirect_output_if_windowless(folder: Path) -> None:
    """Con pythonw.exe (Windows, sin consola) sys.stdout y sys.stderr son None: cualquier print o
    traceback haría fallar el programa. Se envían a «registro.log» en la carpeta de datos."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "registro.log"
    if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
        log.replace(folder / "registro.anterior.log")
    stream = open(log, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


def main():
    port = int(os.environ.get("INVENTARIO_PUERTO", DEFAULT_PORT))
    folder = data_dir()
    redirect_output_if_windowless(folder)
    url = f"http://127.0.0.1:{port}/"
    profile = folder / "ventana"
    if running(port):
        open_window(url, profile)
        return
    server = create_server(folder, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Inventario Inteligente en {url} (datos: {folder}). Cierre la ventana para salir.", flush=True)
    window = open_window(url, profile)
    try:
        if window is None:
            while True:
                time.sleep(3600)
        else:
            # El navegador puede delegar en un proceso ya abierto con el mismo perfil
            # y salir enseguida: en ese caso se sigue sirviendo hasta Ctrl+C.
            started = time.monotonic()
            window.wait()
            if time.monotonic() - started < 5:
                while True:
                    time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        server.app.close()


if __name__ == "__main__":
    main()
