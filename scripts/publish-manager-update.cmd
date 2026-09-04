@echo off
setlocal
echo [ERROR] This CMD entry cannot safely forward Chinese release notes from PowerShell.
echo Run the PowerShell entry instead:
echo   .\scripts\publish-manager-update.ps1 [arguments]
exit /b 2
