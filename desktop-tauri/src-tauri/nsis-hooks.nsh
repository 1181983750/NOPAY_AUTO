; Tauri's generated uninstaller removes the main executable, but resource
; folders are not recursively removed by default. Keep the Python sidecar
; recoverable during normal use and remove it only during an explicit uninstall.
!macro NSIS_HOOK_PREUNINSTALL
  RMDir /r "$INSTDIR\nopay-python"
!macroend
