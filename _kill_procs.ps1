Stop-Process -Name qjd67-canvas-bot -Force -ErrorAction SilentlyContinue
Stop-Process -Name python -Force -ErrorAction SilentlyContinue
Stop-Process -Name 'NO钱包自动化' -Force -ErrorAction SilentlyContinue
Get-Process | Where-Object { $_.Name -match 'canvas-bot|python|NO钱包' } | Select-Object Name, Id
