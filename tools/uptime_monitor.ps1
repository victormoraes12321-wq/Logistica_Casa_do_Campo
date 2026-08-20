param(
  [string]$HealthUrl = "http://127.0.0.1:3000/healthz",
  [int]$IntervalSeconds = 30
)

$ErrorActionPreference = "SilentlyContinue"
$baseDir = Split-Path -Parent $PSScriptRoot
$logsDir = Join-Path $baseDir "logs"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
$logPath = Join-Path $logsDir "uptime_monitor.log"

while ($true) {
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  try {
    $resp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 10
    $status = [int]$resp.StatusCode
    Add-Content -Path $logPath -Value "[$stamp] HEALTH status=$status url=$HealthUrl"
  }
  catch {
    Add-Content -Path $logPath -Value "[$stamp] HEALTH status=DOWN url=$HealthUrl msg=$($_.Exception.Message)"
  }
  Start-Sleep -Seconds $IntervalSeconds
}

