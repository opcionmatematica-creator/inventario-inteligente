; Instalador de Inventario Inteligente para Windows (NSIS 3, Unicode).
; Lo genera instalar/windows/construir_instalador.sh: no se compila a mano.
; Parámetros: -DVERSION=0.2.0 -DVERSION4=0.2.0.0 -DROOT=<carpeta preparada> -DOUT=<setup.exe>

Unicode true
SetCompressor /SOLID lzma
!include "MUI2.nsh"

!define APP "Inventario Inteligente"
!define KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\InventarioInteligente"

Name "${APP} ${VERSION}"
OutFile "${OUT}"
; Por usuario: sin permisos de administrador ni aviso UAC. Los datos del comercio van aparte,
; en %LOCALAPPDATA%\InventarioInteligente, y sobreviven a actualizaciones y desinstalación.
InstallDir "$LOCALAPPDATA\Programs\${APP}"
InstallDirRegKey HKCU "${KEY}" "InstallLocation"
RequestExecutionLevel user
ShowInstDetails nevershow
BrandingText "${APP} ${VERSION}"

VIProductVersion "${VERSION4}"
VIAddVersionKey /LANG=1034 "ProductName" "${APP}"
VIAddVersionKey /LANG=1034 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1034 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1034 "FileDescription" "Instalador de ${APP}"
VIAddVersionKey /LANG=1034 "LegalCopyright" "Licencia PolyForm Noncommercial 1.0.0; uso comercial con licencia"

!define MUI_ICON "${ROOT}\inventario.ico"
!define MUI_UNICON "${ROOT}\inventario.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "Este asistente instala ${APP} ${VERSION}: caja e inventario para su comercio que funcionan sin internet.$\r$\n$\r$\nNo necesita Python ni otros programas. Sus datos se guardan en este equipo.$\r$\n$\r$\nImportante: el programa no emite factura electrónica DIAN."
!define MUI_LICENSEPAGE_TEXT_TOP "Lea la licencia. Probarlo y el uso no comercial son gratuitos; el uso en un negocio requiere una licencia comercial."
!define MUI_FINISHPAGE_RUN "$INSTDIR\python\pythonw.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS "-m inventario.app"
!define MUI_FINISHPAGE_RUN_TEXT "Abrir ${APP}"
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\Manual de usuario.txt"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Ver el manual de usuario"
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${ROOT}\Licencia.txt"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Spanish"

Section "Programa" SecPrograma
  SectionIn RO
  SetOutPath "$INSTDIR"
  ; Una actualización reemplaza el programa, nunca los datos (están en otra carpeta).
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\python"
  File /r "${ROOT}\*.*"

  ; El acceso arranca el servidor local y abre la ventana (Edge en modo aplicación).
  SetOutPath "$INSTDIR"
  CreateDirectory "$SMPROGRAMS\${APP}"
  CreateShortcut "$SMPROGRAMS\${APP}\${APP}.lnk" "$INSTDIR\python\pythonw.exe" "-m inventario.app" "$INSTDIR\inventario.ico" 0 SW_SHOWNORMAL "" "Caja e inventario sin internet"
  CreateShortcut "$SMPROGRAMS\${APP}\Manual de usuario.lnk" "$INSTDIR\Manual de usuario.txt"
  CreateShortcut "$SMPROGRAMS\${APP}\Desinstalar.lnk" "$INSTDIR\Desinstalar.exe"
  CreateShortcut "$DESKTOP\${APP}.lnk" "$INSTDIR\python\pythonw.exe" "-m inventario.app" "$INSTDIR\inventario.ico" 0 SW_SHOWNORMAL "" "Caja e inventario sin internet"

  WriteUninstaller "$INSTDIR\Desinstalar.exe"
  WriteRegStr HKCU "${KEY}" "DisplayName" "${APP}"
  WriteRegStr HKCU "${KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${KEY}" "Publisher" "${APP}"
  WriteRegStr HKCU "${KEY}" "DisplayIcon" "$INSTDIR\inventario.ico"
  WriteRegStr HKCU "${KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${KEY}" "UninstallString" '"$INSTDIR\Desinstalar.exe"'
  WriteRegStr HKCU "${KEY}" "QuietUninstallString" '"$INSTDIR\Desinstalar.exe" /S'
  WriteRegDWORD HKCU "${KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${KEY}" "NoRepair" 1
  WriteRegDWORD HKCU "${KEY}" "EstimatedSize" 30000
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\${APP}.lnk"
  RMDir /r "$SMPROGRAMS\${APP}"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\python"
  Delete "$INSTDIR\*.txt"
  Delete "$INSTDIR\inventario.ico"
  Delete "$INSTDIR\Desinstalar.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${KEY}"
  ; Los datos del comercio NO se borran.
  IfSilent +2
    MessageBox MB_OK|MB_ICONINFORMATION "${APP} se desinstaló.$\r$\n$\r$\nSus datos (productos, ventas y respaldos) se conservan en:$\r$\n$LOCALAPPDATA\InventarioInteligente$\r$\n$\r$\nSi vuelve a instalar el programa, los encontrará igual."
SectionEnd
