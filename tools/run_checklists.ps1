param(
  [ValidateSet("go-live","post-reboot","all")]
  [string]$Mode = "all",
  [string]$BaseUrl = "http://127.0.0.1:3000",
  [switch]$FullGate
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $PSCommandPath
$baseDir = Split-Path -Parent $scriptDir
$logsDir = Join-Path $baseDir "logs\checklists"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$reportPath = Join-Path $logsDir "checklist_$Mode`_$stamp.md"

function Run-Step {
  param(
    [string]$Name,
    [scriptblock]$Action
  )
  try {
    & $Action
    return [pscustomobject]@{ Step = $Name; Status = "OK"; Detail = "" }
  } catch {
    return [pscustomobject]@{ Step = $Name; Status = "FALHA"; Detail = $_.Exception.Message }
  }
}

function Run-Cmd([string]$Command) {
  powershell -NoProfile -Command $Command
  if ($LASTEXITCODE -ne 0) { throw "Comando falhou: $Command" }
}

$results = @()

if ($Mode -in @("go-live","all")) {
  $results += Run-Step "Sintaxe Python" { Run-Cmd "python -m py_compile app.py run.py" }
  $results += Run-Step "Integridade de banco" { Run-Cmd "python tools\db_integrity_audit.py" }
  $results += Run-Step "Congelar matriz de permissoes" { Run-Cmd "python tools\export_permission_matrix.py" }
  $results += Run-Step "Revisao semanal de auditoria" { Run-Cmd "python tools\audit_log_weekly_review.py --days 7" }
  $results += Run-Step "Simulacao de restauracao" { Run-Cmd "python tools\simular_restauracao_desastre.py" }
  if ($FullGate) {
    $results += Run-Step "Gate completo de regressao" { Run-Cmd "python tools\regression_release_gate.py" }
  }
}

if ($Mode -in @("post-reboot","all")) {
  $results += Run-Step "Healthcheck HTTP" {
    $res = Invoke-WebRequest -Uri "$BaseUrl/healthz" -UseBasicParsing -TimeoutSec 8
    if ($res.StatusCode -ne 200) { throw "Healthcheck retornou $($res.StatusCode)" }
  }
  $results += Run-Step "Status tarefas Windows" { Run-Cmd "powershell -ExecutionPolicy Bypass -File tools\install_windows_tasks.ps1 -StatusOnly" }
  $results += Run-Step "Limite de backups (<=7)" {
    $backupDir = Join-Path $baseDir "backups"
    if (-not (Test-Path $backupDir)) { throw "Pasta backups nao encontrada." }
    $count = (Get-ChildItem -Path $backupDir -File -Filter *.sqlite3 | Measure-Object).Count
    if ($count -gt 7) { throw "Existem $count backups; limite esperado <= 7." }
  }
}

$okCount = ($results | Where-Object { $_.Status -eq "OK" }).Count
$failCount = ($results | Where-Object { $_.Status -eq "FALHA" }).Count
$nowLabel = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

$lines = @()
$lines += "# Checklist operacional ($Mode)"
$lines += ""
$lines += "- Gerado em: **$nowLabel**"
$lines += "- Base URL: **$BaseUrl**"
$lines += "- Passos OK: **$okCount**"
$lines += "- Passos com falha: **$failCount**"
$lines += ""
$lines += "## Resultado por passo"
foreach ($r in $results) {
  if ($r.Status -eq "OK") {
    $lines += "- [OK] $($r.Step)"
  } else {
    $lines += "- [FALHA] $($r.Step): $($r.Detail)"
  }
}
$lines += ""
$lines += "Relatorio salvo em: $reportPath"

Set-Content -Path $reportPath -Value ($lines -join "`r`n") -Encoding UTF8
Write-Host ""
Write-Host "Checklist concluido: $Mode" -ForegroundColor Cyan
Write-Host "OK: $okCount | FALHA: $failCount"
Write-Host "Relatorio: $reportPath"

if ($failCount -gt 0) { exit 1 }
exit 0
