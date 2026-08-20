param(
  [switch]$RestartAfter
)

$ErrorActionPreference = "Stop"

function Stop-TaskSafe([string]$TaskName) {
  try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
}

function Start-TaskSafe([string]$TaskName) {
  try { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
}

Stop-TaskSafe "LogisticaCasaDoCampo-UptimeMonitor"
Stop-TaskSafe "LogisticaCasaDoCampo-Watchdog"
Start-Sleep -Seconds 2

$apps = Get-CimInstance Win32_Process -Filter "name='python.exe'" | Where-Object { $_.CommandLine -match "app.py" }
foreach ($p in $apps) {
  try {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
    Write-Host "Processo app.py encerrado: PID $($p.ProcessId)"
  } catch {}
}
Start-Sleep -Seconds 2

$targets = @(
  "data\logistica_casa_do_campo.sqlite3-wal",
  "data\logistica_casa_do_campo.sqlite3-shm",
  "data\logistica_casa_do_campo.sqlite3-journal",
  "data\relatorio_pedidos.csv"
)

foreach ($file in $targets) {
  if (Test-Path $file) {
    Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
  }
}

Write-Host "Limpeza pre-empacotamento concluida."

if ($RestartAfter) {
  Start-TaskSafe "LogisticaCasaDoCampo-Watchdog"
  Start-Sleep -Seconds 4
  Start-TaskSafe "LogisticaCasaDoCampo-UptimeMonitor"
  Write-Host "Watchdog e monitor reiniciados."
}
