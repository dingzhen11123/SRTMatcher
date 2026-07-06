Unicode true
!include "MUI2.nsh"
Caption "SRTMatcher Setup"
BrandingText "SRTMatcher"
Name "SRTMatcher"
OutFile "dist\SRTMatcherSetup.exe"
InstallDir "$LOCALAPPDATA\SRTMatcher"
InstallDirRegKey HKCU "Software\SRTMatcher" "InstallDir"
RequestExecutionLevel user

!define APP_NAME "SRTMatcher"
!define APP_EXE "SRTMatcher.exe"
!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Section "Install"
  SetOverwrite on
  SetOutPath "$INSTDIR"
  File "dist\nsis_payload\SRTMatcher.exe"

  WriteRegStr HKCU "Software\SRTMatcher" "InstallDir" "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\SRTMatcher"
  CreateShortcut "$DESKTOP\SRTMatcher.lnk" "$INSTDIR\${APP_EXE}" "--launcher" "$INSTDIR\${APP_EXE}" 0
  CreateShortcut "$SMPROGRAMS\SRTMatcher\SRTMatcher.lnk" "$INSTDIR\${APP_EXE}" "--launcher" "$INSTDIR\${APP_EXE}" 0
  CreateShortcut "$SMPROGRAMS\SRTMatcher\卸载 SRTMatcher.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\SRTMatcher.lnk"
  Delete "$SMPROGRAMS\SRTMatcher\SRTMatcher.lnk"
  Delete "$SMPROGRAMS\SRTMatcher\卸载 SRTMatcher.lnk"
  RMDir "$SMPROGRAMS\SRTMatcher"

  Delete "$INSTDIR\SRTMatcher.exe"
  Delete "$INSTDIR\SRTMatcher.bat"
  Delete "$INSTDIR\.runtime-ready"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\tools"
  RMDir /r "$INSTDIR\.venv"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "Software\SRTMatcher"
SectionEnd
