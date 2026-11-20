@echo off
rem Abre Inventario Inteligente (servidor local + ventana). Requiere Python 3.11+.
cd /d "%~dp0..\.."
where pyw >nul 2>nul && (start "" pyw -3 -m inventario.app & exit /b)
where pythonw >nul 2>nul && (start "" pythonw -m inventario.app & exit /b)
echo Hace falta Python 3.11 o posterior: https://www.python.org/downloads/windows/
pause
