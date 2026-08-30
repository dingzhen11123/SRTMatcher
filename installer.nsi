Unicode true
!include "MUI2.nsh"
!define MUI_ICON "srtmatcher.ico"
!define MUI_UNICON "srtmatcher.ico"
Caption "字幕多功能工具（SRTMatcher）安装"
BrandingText "字幕多功能工具 · SRTMatcher"
Name "字幕多功能工具（SRTMatcher）"
!ifndef OUTPUT_FILE
  !define OUTPUT_FILE "dist\SRTMatcherSetup.exe"
!endif
OutFile "${OUTPUT_FILE}"
Icon "srtmatcher.ico"
UninstallIcon "srtmatcher.ico"
InstallDir "$LOCALAPPDATA\SRTMatcher"
InstallDirRegKey HKCU "Software\SRTMatcher" "InstallDir"
RequestExecutionLevel user

!define APP_NAME "SRTMatcher"
!define DISPLAY_NAME "字幕多功能工具"
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
  ; 强制下一次启动核对并刷新内置源码，避免旧版本标记跳过覆盖升级。
  Delete "$INSTDIR\.runtime-ready"
  File "dist\nsis_payload\SRTMatcher.exe"
  File "/oname=srtmatcher.ico" "srtmatcher.ico"

  SetOutPath "$INSTDIR\app"
  File "dist\nsis_payload\ffmpeg.exe"
  SetOutPath "$INSTDIR"

  WriteRegStr HKCU "Software\SRTMatcher" "InstallDir" "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  Delete "$DESKTOP\SRTMatcher.lnk"
  RMDir /r "$SMPROGRAMS\SRTMatcher"
  CreateDirectory "$SMPROGRAMS\${DISPLAY_NAME}"
  CreateShortcut "$DESKTOP\${DISPLAY_NAME}.lnk" "$INSTDIR\${APP_EXE}" "--launcher" "$INSTDIR\srtmatcher.ico" 0
  CreateShortcut "$SMPROGRAMS\${DISPLAY_NAME}\${DISPLAY_NAME}.lnk" "$INSTDIR\${APP_EXE}" "--launcher" "$INSTDIR\srtmatcher.ico" 0
  CreateShortcut "$SMPROGRAMS\${DISPLAY_NAME}\卸载 ${DISPLAY_NAME}.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\${DISPLAY_NAME}.lnk"
  Delete "$DESKTOP\SRTMatcher.lnk"
  RMDir /r "$SMPROGRAMS\${DISPLAY_NAME}"
  RMDir /r "$SMPROGRAMS\SRTMatcher"

  Delete "$INSTDIR\SRTMatcher.exe"
  Delete "$INSTDIR\SRTMatcher.bat"
  Delete "$INSTDIR\srtmatcher.ico"
  Delete "$INSTDIR\.runtime-ready"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\tools"
  RMDir /r "$INSTDIR\.venv"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "Software\SRTMatcher"
SectionEnd
