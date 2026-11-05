Unicode true
ManifestDPIAware true
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "LogicLib.nsh"

!define APP_NAME "SRTMatcher"
!define DISPLAY_NAME "字幕多功能工具"
!define APP_EXE "SRTMatcher.exe"
!define PUBLISHER "SRTMatcher"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
!define ASSETS "build\installer-assets"
!ifndef OUTPUT_FILE
  !define OUTPUT_FILE "dist\SRTMatcherSetup.exe"
!endif
!ifndef APP_VERSION
  !define APP_VERSION "1.0.0.0"
!endif
!ifndef BUILD_ID
  !define BUILD_ID "dev"
!endif

Name "${DISPLAY_NAME}"
Caption "${DISPLAY_NAME} 安装程序"
BrandingText "${DISPLAY_NAME} · ${BUILD_ID}"
OutFile "${OUTPUT_FILE}"
Icon "srtmatcher.ico"
UninstallIcon "srtmatcher.ico"
InstallDir "$LOCALAPPDATA\${APP_NAME}"
InstallDirRegKey HKCU "Software\${APP_NAME}" "InstallDir"
RequestExecutionLevel user
ShowInstDetails hide
ShowUninstDetails hide

VIProductVersion "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "ProductName" "${DISPLAY_NAME}"
VIAddVersionKey /LANG=2052 "FileDescription" "${DISPLAY_NAME} 安装程序"
VIAddVersionKey /LANG=2052 "CompanyName" "${PUBLISHER}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "${PUBLISHER}"
VIAddVersionKey /LANG=2052 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion" "${BUILD_ID}"

Var IsUpgrade
Var WelcomeText

; ---- 界面 ------------------------------------------------------------------
!define MUI_ICON "srtmatcher.ico"
!define MUI_UNICON "srtmatcher.ico"
!define MUI_ABORTWARNING
!define MUI_ABORTWARNING_TEXT "确定要退出${DISPLAY_NAME}安装吗？"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "${ASSETS}\header.bmp"
!define MUI_HEADERIMAGE_UNBITMAP "${ASSETS}\header.bmp"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${ASSETS}\wizard.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "${ASSETS}\wizard.bmp"

!define MUI_WELCOMEPAGE_TITLE "欢迎使用${DISPLAY_NAME}"
!define MUI_WELCOMEPAGE_TEXT "$WelcomeText"
!insertmacro MUI_PAGE_WELCOME

!define MUI_PAGE_CUSTOMFUNCTION_PRE DirectoryPre
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE DirectoryLeave
!define MUI_DIRECTORYPAGE_TEXT_TOP "选择${DISPLAY_NAME}的安装位置。建议放在有足够空间的磁盘：运行环境约 8 GB，模型约 2 GB。"
!insertmacro MUI_PAGE_DIRECTORY

!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "安装完成"
!define MUI_FINISHPAGE_TEXT "${DISPLAY_NAME}已安装到：$\r$\n$INSTDIR$\r$\n$\r$\n首次启动会自动准备运行环境；CUDA 加速和语音模型可在软件的“设置”页一键补齐。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_PARAMETERS "--launcher"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动${DISPLAY_NAME}"
!define MUI_FINISHPAGE_NOREBOOTSUPPORT
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
; 语言文件默认用宋体，换成与主程序一致的微软雅黑。
SetFont /LANG=${LANG_SIMPCHINESE} "Microsoft YaHei UI" 9

; ---- 安装 ------------------------------------------------------------------
Function .onInit
  StrCpy $IsUpgrade 0
  ReadRegStr $0 HKCU "Software\${APP_NAME}" "InstallDir"
  ${If} $0 != ""
  ${AndIf} ${FileExists} "$0\${APP_EXE}"
    StrCpy $IsUpgrade 1
    StrCpy $INSTDIR $0
  ${EndIf}
  ; 静默安装不经过目录页，/D= 指定的目录也要走同样的保护。
  ${If} ${Silent}
  ${AndIf} $IsUpgrade == 0
    Call EnsureOwnInstallDir
  ${EndIf}
  ${If} $IsUpgrade == 1
    StrCpy $WelcomeText "检测到已安装的版本：$\r$\n$INSTDIR$\r$\n$\r$\n本次将直接覆盖升级，设置、已下载的模型和运行环境都会保留，无需重新配置。$\r$\n$\r$\n如果软件正在运行，请先关闭它，再点击“下一步”。"
  ${Else}
    StrCpy $WelcomeText "安装向导将引导你完成${DISPLAY_NAME}的安装。$\r$\n$\r$\n安装包本身很小，不内置大模型和 CUDA；首次启动时会自动准备运行环境，模型可在软件设置页下载。$\r$\n$\r$\n点击“下一步”继续。"
  ${EndIf}
FunctionEnd

Function DirectoryPre
  ; 覆盖升级沿用原安装目录，跳过目录页。
  ${If} $IsUpgrade == 1
    Abort
  ${EndIf}
FunctionEnd

Function EnsureOwnInstallDir
  ; 统一装进名为 SRTMatcher 的子目录，避免直接装进 D:\ 之类的共享目录，
  ; 卸载时误删同级的 app / tools 文件夹。
  StrCpy $0 $INSTDIR "" -11
  ${If} $0 != "\${APP_NAME}"
    StrCpy $INSTDIR "$INSTDIR\${APP_NAME}"
  ${EndIf}
FunctionEnd

Function DirectoryLeave
  Call EnsureOwnInstallDir
  ClearErrors
  CreateDirectory "$INSTDIR"
  FileOpen $1 "$INSTDIR\.write-test" w
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONSTOP "没有权限写入以下目录，请换一个位置（例如 D:\${APP_NAME}）：$\r$\n$INSTDIR"
    Abort
  ${EndIf}
  FileClose $1
  Delete "$INSTDIR\.write-test"
FunctionEnd

!macro EnsureNotLocked FILE
  ${If} ${FileExists} "${FILE}"
    ${Do}
      ClearErrors
      FileOpen $0 "${FILE}" a
      ${IfNot} ${Errors}
        FileClose $0
        ${Break}
      ${EndIf}
      ${If} ${Silent}
        DetailPrint "文件被占用: ${FILE}"
        SetErrorLevel 2
        Abort "${DISPLAY_NAME}正在运行，无法覆盖升级。"
      ${EndIf}
      ${IfNot} ${Cmd} `MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "${DISPLAY_NAME}正在运行，请先关闭它，然后点击“重试”。" IDRETRY`
        Abort "安装已取消：${DISPLAY_NAME}正在运行。"
      ${EndIf}
    ${Loop}
  ${EndIf}
!macroend

Section "Install"
  SetOverwrite on
  SetOutPath "$INSTDIR"
  !insertmacro EnsureNotLocked "$INSTDIR\${APP_EXE}"
  !insertmacro EnsureNotLocked "$INSTDIR\app\ffmpeg.exe"

  DetailPrint "正在复制程序文件 ..."
  ; 强制下一次启动核对并刷新内置源码，避免旧版本标记跳过覆盖升级。
  Delete "$INSTDIR\.runtime-ready"
  File "dist\nsis_payload\${APP_EXE}"
  File "/oname=srtmatcher.ico" "srtmatcher.ico"

  SetOutPath "$INSTDIR\app"
  File "dist\nsis_payload\ffmpeg.exe"
  SetOutPath "$INSTDIR"

  DetailPrint "正在创建快捷方式 ..."
  WriteRegStr HKCU "Software\${APP_NAME}" "InstallDir" "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  Delete "$DESKTOP\${APP_NAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APP_NAME}"
  CreateDirectory "$SMPROGRAMS\${DISPLAY_NAME}"
  CreateShortcut "$DESKTOP\${DISPLAY_NAME}.lnk" "$INSTDIR\${APP_EXE}" "--launcher" "$INSTDIR\srtmatcher.ico" 0
  CreateShortcut "$SMPROGRAMS\${DISPLAY_NAME}\${DISPLAY_NAME}.lnk" "$INSTDIR\${APP_EXE}" "--launcher" "$INSTDIR\srtmatcher.ico" 0
  CreateShortcut "$SMPROGRAMS\${DISPLAY_NAME}\卸载${DISPLAY_NAME}.lnk" "$INSTDIR\Uninstall.exe"

  ; 在“设置 → 应用”里显示名称、图标、版本和卸载入口。
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${DISPLAY_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\srtmatcher.ico"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${BUILD_ID}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

; ---- 卸载 ------------------------------------------------------------------
Function un.onInit
  ${IfNot} ${FileExists} "$INSTDIR\${APP_EXE}"
    MessageBox MB_OK|MB_ICONSTOP "没有在以下目录找到${DISPLAY_NAME}，已取消卸载：$\r$\n$INSTDIR"
    Abort
  ${EndIf}
FunctionEnd

Section "Uninstall"
  Delete "$DESKTOP\${DISPLAY_NAME}.lnk"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  RMDir /r "$SMPROGRAMS\${DISPLAY_NAME}"
  RMDir /r "$SMPROGRAMS\${APP_NAME}"

  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\SRTMatcher.bat"
  Delete "$INSTDIR\srtmatcher.ico"
  Delete "$INSTDIR\.runtime-ready"
  Delete "$INSTDIR\.requirements-installed"
  Delete "$INSTDIR\launcher.log"
  Delete "$INSTDIR\app-stderr.log"
  Delete "$INSTDIR\Uninstall.exe"
  ; 旧版本可能装在名字不是 SRTMatcher 的共享目录里，那里的子目录不一定属于本软件。
  StrCpy $0 $INSTDIR "" -11
  StrCpy $1 1
  ${If} $0 != "\${APP_NAME}"
    StrCpy $1 0
    ${IfNot} ${Silent}
      ${If} ${Cmd} `MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "安装目录不是专用的 ${APP_NAME} 文件夹：$\r$\n$INSTDIR$\r$\n$\r$\n是否删除其中的 app、tools、.venv 子目录？请先确认它们不属于其他程序。" IDYES`
        StrCpy $1 1
      ${EndIf}
    ${EndIf}
  ${EndIf}
  ${If} $1 == 1
    RMDir /r "$INSTDIR\app"
    RMDir /r "$INSTDIR\tools"
    RMDir /r "$INSTDIR\.venv"
  ${EndIf}

  ${If} ${FileExists} "$INSTDIR\models\*.*"
    ${IfNot} ${Silent}
      MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "是否同时删除已下载的语音模型（$INSTDIR\models）？$\r$\n$\r$\n保留模型的话，下次重新安装可以直接使用。" IDNO keep_models
      RMDir /r "$INSTDIR\models"
      keep_models:
    ${EndIf}
  ${EndIf}
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\${APP_NAME}"
SectionEnd
