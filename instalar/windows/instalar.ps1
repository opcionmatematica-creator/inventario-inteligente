# Crea accesos «Inventario Inteligente» en el menú Inicio y en el escritorio (Windows).
# Uso (PowerShell): powershell -ExecutionPolicy Bypass -File instalar\windows\instalar.ps1
$ErrorActionPreference = "Stop"
$raiz = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = (Get-Command pyw -ErrorAction SilentlyContinue)
if (-not $python) { $python = (Get-Command pythonw -ErrorAction SilentlyContinue) }
if (-not $python) {
    Write-Host "Hace falta Python 3.11 o posterior. Instálelo con: winget install Python.Python.3.12"
    exit 1
}
$argumentos = if ($python.Name -like "pyw*") { "-3 -m inventario.app" } else { "-m inventario.app" }
# Python de Windows no trae la base de zonas horarias que usa el programa (America/Bogota).
$py = if ($python.Name -like "pyw*") { "py" } else { $python.Source -replace "pythonw.exe$", "python.exe" }
& $py -m pip install --user --quiet tzdata
if ($LASTEXITCODE -ne 0) { Write-Host "No se pudo instalar tzdata (hace falta internet la primera vez)."; exit 1 }
$shell = New-Object -ComObject WScript.Shell
foreach ($carpeta in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
    $acceso = $shell.CreateShortcut((Join-Path $carpeta "Inventario Inteligente.lnk"))
    $acceso.TargetPath = $python.Source
    $acceso.Arguments = $argumentos
    $acceso.WorkingDirectory = $raiz
    $acceso.Description = "Caja e inventario sin internet"
    $acceso.Save()
}
Write-Host "Listo: use el acceso «Inventario Inteligente» del escritorio o del menú Inicio."
Write-Host "Los datos se guardan en $env:LOCALAPPDATA\InventarioInteligente"
