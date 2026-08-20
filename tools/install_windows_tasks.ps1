param(
  [string]$TaskPrefix = "LogisticaCasaDoCampo",
  [string]$BindHost = "0.0.0.0",
  [int]$Port = 3000,
  [string]$DailyBackupAt = "02:00",
  [string]$WeeklyVerifyAt = "03:00",
  [string]$WeeklyAuditAt = "03:20",
  [string]$WeeklyVerifyDay = "Sunday",
  [int]$KeepDays = 30,
  [int]$KeepMin = 7,
  [int]$KeepMax = 7,
  [switch]$Force,
  [switch]$Uninstall,
  [switch]$StatusOnly
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $PSCommandPath
$baseDir = Split-Path -Parent $scriptDir
$logsDir = Join-Path $baseDir "logs"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
$logPath = Join-Path $logsDir "task_install.log"

function Write-InstallLog([string]$Message) {
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -Path $logPath -Value "[$stamp] $Message"
}

function Find-PythonExe {
  if ($env:LOGISTICA_PYTHON_EXE -and (Test-Path $env:LOGISTICA_PYTHON_EXE)) {
    try {
      $test = & $env:LOGISTICA_PYTHON_EXE -c "import sys, socket" 2>&1
      if ($LASTEXITCODE -eq 0) { return $env:LOGISTICA_PYTHON_EXE }
    } catch {}
  }
  $candidates = @(
    "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
    "C:\Python314\python.exe",
    "C:\Python313\python.exe",
    "C:\Python312\python.exe",
    "C:\Python311\python.exe",
    "C:\Python310\python.exe"
  )
  foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path $candidate)) {
      try {
        $test = & $candidate -c "import sys, socket" 2>&1
        if ($LASTEXITCODE -eq 0) { return $candidate }
      } catch {}
    }
  }
  try {
    $test = py -3 -c "import sys, socket" 2>&1
    if ($LASTEXITCODE -eq 0) { return "py" }
  } catch {}
  try {
    $cmd = Get-Command python -ErrorAction Stop
    if ($cmd.Source -and $cmd.Source -notlike "*WindowsApps*") {
      $test = & $cmd.Source -c "import sys, socket" 2>&1
      if ($LASTEXITCODE -eq 0) { return $cmd.Source }
    }
  } catch {}
  return $null
}

$watchdogTaskName = "$TaskPrefix-Watchdog"
$backupTaskName = "$TaskPrefix-BackupDaily"
$verifyTaskName = "$TaskPrefix-BackupVerifyWeekly"
$auditReviewTaskName = "$TaskPrefix-AuditReviewWeekly"
$uptimeTaskName = "$TaskPrefix-UptimeMonitor"

function Get-TaskState([string]$TaskName) {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $t) {
    return [pscustomobject]@{
      TaskName = $TaskName
      Exists = $false
      State = "Nao instalada"
      LastRunTime = ""
      LastTaskResult = ""
      NextRunTime = ""
    }
  }
  $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
  return [pscustomobject]@{
    TaskName = $TaskName
    Exists = $true
    State = [string]$t.State
    LastRunTime = [string]$info.LastRunTime
    LastTaskResult = [string]$info.LastTaskResult
    NextRunTime = [string]$info.NextRunTime
  }
}

function Remove-TaskIfExists([string]$TaskName) {
  try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-InstallLog "Tarefa removida: $TaskName"
  }
  catch {
  }
}

function Get-CurrentUserIdentity {
  if ($env:USERDOMAIN -and $env:USERNAME) {
    return "$($env:USERDOMAIN)\$($env:USERNAME)"
  }
  return $env:USERNAME
}

function Register-LogisticaTasks([object]$Principal, [object]$WatchdogTrigger, [object]$TaskSettings) {
  Register-ScheduledTask -TaskName $watchdogTaskName -Action $watchdogAction -Trigger $WatchdogTrigger -Principal $Principal -Settings $TaskSettings -Description "Inicia watchdog do Logistica Casa do Campo ao ligar o Windows." -Force | Out-Null
  Register-ScheduledTask -TaskName $uptimeTaskName -Action $uptimeAction -Trigger $WatchdogTrigger -Principal $Principal -Settings $TaskSettings -Description "Monitora uptime e healthcheck do Logistica Casa do Campo." -Force | Out-Null
  Register-ScheduledTask -TaskName $backupTaskName -Action $backupAction -Trigger $backupTrigger -Principal $Principal -Settings $TaskSettings -Description "Gera backup diario com retencao automatica do Logistica Casa do Campo." -Force | Out-Null
  Register-ScheduledTask -TaskName $verifyTaskName -Action $verifyAction -Trigger $verifyTrigger -Principal $Principal -Settings $TaskSettings -Description "Executa validacao semanal de restauracao do ultimo backup." -Force | Out-Null
  Register-ScheduledTask -TaskName $auditReviewTaskName -Action $auditReviewAction -Trigger $auditReviewTrigger -Principal $Principal -Settings $TaskSettings -Description "Executa revisao semanal dos logs de auditoria (acoes criticas/permissoes)." -Force | Out-Null
}

if ($StatusOnly) {
  $states = @(
    Get-TaskState -TaskName $watchdogTaskName
    Get-TaskState -TaskName $uptimeTaskName
    Get-TaskState -TaskName $backupTaskName
    Get-TaskState -TaskName $verifyTaskName
    Get-TaskState -TaskName $auditReviewTaskName
  )
  Write-Host ""
  Write-Host "Status das tarefas do Logistica Casa do Campo" -ForegroundColor Cyan
  $states | Format-Table TaskName, Exists, State, LastRunTime, LastTaskResult, NextRunTime -AutoSize
  Write-Host ""
  Write-Host "Log da instalacao: $logPath"
  exit 0
}

if ($Uninstall) {
  foreach ($taskName in @($watchdogTaskName, $uptimeTaskName, $backupTaskName, $verifyTaskName, $auditReviewTaskName)) {
    Remove-TaskIfExists -TaskName $taskName
  }
  Write-Host ""
  Write-Host "Tarefas removidas (quando existiam)." -ForegroundColor Yellow
  Write-Host " - $watchdogTaskName"
  Write-Host " - $uptimeTaskName"
  Write-Host " - $backupTaskName"
  Write-Host " - $verifyTaskName"
  Write-Host " - $auditReviewTaskName"
  Write-Host ""
  Write-Host "Log da instalacao: $logPath"
  exit 0
}

$pythonExe = Find-PythonExe
if (-not $pythonExe) {
  throw "Python nao encontrado. Instale Python 3.10+ antes de criar as tarefas."
}

$watchdogScript = Join-Path $scriptDir "watchdog.ps1"
$uptimeScript = Join-Path $scriptDir "uptime_monitor.ps1"
$backupScript = Join-Path $scriptDir "backup_automation.py"
$auditReviewScript = Join-Path $scriptDir "audit_log_weekly_review.py"
if (-not (Test-Path $watchdogScript)) { throw "watchdog.ps1 nao encontrado." }
if (-not (Test-Path $uptimeScript)) { throw "uptime_monitor.ps1 nao encontrado." }
if (-not (Test-Path $backupScript)) { throw "backup_automation.py nao encontrado." }
if (-not (Test-Path $auditReviewScript)) { throw "audit_log_weekly_review.py nao encontrado." }

$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$watchdogTrigger = New-ScheduledTaskTrigger -AtStartup
try {
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -Hidden
}
catch {
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12)
}

$watchdogAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$watchdogScript`" -BindHost `"$BindHost`" -Port $Port -RestartDelaySeconds 5"
$uptimeAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$uptimeScript`" -HealthUrl `"http://127.0.0.1:$Port/healthz`" -IntervalSeconds 30"

$backupAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$backupScript`" --mode backup --keep-days $KeepDays --keep-min $KeepMin --keep-max $KeepMax"
$backupTrigger = New-ScheduledTaskTrigger -Daily -At $DailyBackupAt

$verifyAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$backupScript`" --mode verify --keep-days $KeepDays --keep-min $KeepMin --keep-max $KeepMax"
$verifyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyVerifyDay -At $WeeklyVerifyAt -WeeksInterval 1

$auditReviewAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$auditReviewScript`" --days 7 --out-dir `"$logsDir\\audit_reviews`""
$auditReviewTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyVerifyDay -At $WeeklyAuditAt -WeeksInterval 1

if ($Force) {
  foreach ($taskName in @($watchdogTaskName, $uptimeTaskName, $backupTaskName, $verifyTaskName, $auditReviewTaskName)) {
    Remove-TaskIfExists -TaskName $taskName
    Write-InstallLog "Tarefa removida (Force): $taskName"
  }
}

try {
  Register-LogisticaTasks -Principal $principal -WatchdogTrigger $watchdogTrigger -TaskSettings $settings
  Write-InstallLog "Tarefas registradas em modo SYSTEM: $watchdogTaskName, $uptimeTaskName, $backupTaskName, $verifyTaskName, $auditReviewTaskName"
  Write-Host ""
  Write-Host "Tarefas instaladas com sucesso (modo SYSTEM):" -ForegroundColor Green
  Write-Host " - $watchdogTaskName (startup)"
  Write-Host " - $uptimeTaskName (startup)"
  Write-Host " - $backupTaskName (diario as $DailyBackupAt | limite: $KeepMax backups)"
  Write-Host " - $verifyTaskName (semanal: $WeeklyVerifyDay as $WeeklyVerifyAt)"
  Write-Host " - $auditReviewTaskName (semanal: $WeeklyVerifyDay as $WeeklyAuditAt)"
  Write-Host ""
  Write-Host "Log da instalacao: $logPath"
}
catch {
  $msg = $_.Exception.Message
  if ($msg -notmatch 'Acesso negado|Access is denied|0x80070005') {
    throw
  }
  Write-InstallLog "Falha em modo SYSTEM (acesso negado). Tentando modo usuario atual."
  foreach ($taskName in @($watchdogTaskName, $uptimeTaskName, $backupTaskName, $verifyTaskName, $auditReviewTaskName)) {
    Remove-TaskIfExists -TaskName $taskName
  }
  $currentUser = Get-CurrentUserIdentity
  $principalUser = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
  $watchdogTriggerUser = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
  Register-LogisticaTasks -Principal $principalUser -WatchdogTrigger $watchdogTriggerUser -TaskSettings $settings
  Write-InstallLog "Tarefas registradas em modo usuario atual: $currentUser"
  Write-Host ""
  Write-Host "Tarefas instaladas com sucesso (modo usuario atual):" -ForegroundColor Yellow
  Write-Host " - $watchdogTaskName (no logon de $currentUser)"
  Write-Host " - $uptimeTaskName (no logon de $currentUser)"
  Write-Host " - $backupTaskName (diario as $DailyBackupAt | limite: $KeepMax backups)"
  Write-Host " - $verifyTaskName (semanal: $WeeklyVerifyDay as $WeeklyVerifyAt)"
  Write-Host " - $auditReviewTaskName (semanal: $WeeklyVerifyDay as $WeeklyAuditAt)"
  Write-Host ""
  Write-Host "Obs: para iniciar antes do logon e em conta SYSTEM, execute o script como administrador."
  Write-Host "Log da instalacao: $logPath"
}
