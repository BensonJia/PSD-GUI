Unicode True
!include "MUI2.nsh"

!ifndef STAGE
  !error "STAGE is required"
!endif
!ifndef OUTPUT
  !error "OUTPUT is required"
!endif

Name "PSD-GUI"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\PSD-GUI"
InstallDirRegKey HKCU "Software\PowerSimulationsDynamics\PSD-GUI" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "${STAGE}\PSD-GUI.ico"
UninstallIcon "${STAGE}\PSD-GUI.ico"

VIProductVersion "0.1.0.0"
VIAddVersionKey /LANG=2052 "ProductName" "PSD-GUI"
VIAddVersionKey /LANG=2052 "CompanyName" "PowerSimulationsDynamics"
VIAddVersionKey /LANG=2052 "FileDescription" "PowerSimulationDynamics-GUI 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "0.1.0"

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\python\pythonw.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS "-I -m psid_graph_viewer"
!define MUI_FINISHPAGE_RUN_TEXT "启动 PSD-GUI"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Section "PSD-GUI" MainSection
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  File /r "${STAGE}\*"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\PowerSimulationsDynamics\PSD-GUI" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PSD-GUI" "DisplayName" "PSD-GUI"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PSD-GUI" "DisplayIcon" "$INSTDIR\PSD-GUI.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PSD-GUI" "DisplayVersion" "0.1.0"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PSD-GUI" "Publisher" "PowerSimulationsDynamics"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PSD-GUI" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PSD-GUI" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PSD-GUI" "NoRepair" 1

  CreateDirectory "$SMPROGRAMS\PSD-GUI"
  CreateShortcut "$SMPROGRAMS\PSD-GUI\PSD-GUI.lnk" "$INSTDIR\python\pythonw.exe" "-I -m psid_graph_viewer" "$INSTDIR\PSD-GUI.ico"
  CreateShortcut "$DESKTOP\PSD-GUI.lnk" "$INSTDIR\python\pythonw.exe" "-I -m psid_graph_viewer" "$INSTDIR\PSD-GUI.ico"
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$DESKTOP\PSD-GUI.lnk"
  RMDir /r "$SMPROGRAMS\PSD-GUI"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PSD-GUI"
  DeleteRegKey HKCU "Software\PowerSimulationsDynamics\PSD-GUI"
  RMDir /r "$INSTDIR"
SectionEnd
