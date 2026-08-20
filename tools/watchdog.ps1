param(
  [string]$BindHost = "0.0.0.0",
  [int]$Port = 3000,
  [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $PSCommandPath
$baseDir = Split-Path -Parent $scriptDir
$logsDir = Join-Path $baseDir "logs"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

$watchdogLog = Join-Path $logsDir "watchdog.log"
$runtimeLog = Join-Path $logsDir "runtime.log"
$runtimeErrLog = Join-Path $logsDir "runtime.err.log"

function Write-WatchdogLog([string]$Message) {
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -Path $watchdogLog -Value "[$stamp] $Message"
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

function Test-PortListening([int]$PortToCheck) {
  try {
    $conn = Get-NetTCPConnection -LocalPort $PortToCheck -State Listen -ErrorAction Stop
    return $conn.Count -gt 0
  }
  catch {
    return (netstat -nao | Select-String ":$PortToCheck\s+.*LISTENING").Count -gt 0
  }
}

function Ensure-FirewallRule([int]$PortToOpen) {
  $ruleName = "Logistica Casa do Campo - Porta $PortToOpen"
  try {
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if (-not $existing) {
      New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $PortToOpen -Profile Private | Out-Null
      Write-WatchdogLog "Regra de firewall criada para porta $PortToOpen (perfil Private)."
    }
  }
  catch {
    Write-WatchdogLog "Nao foi possivel criar regra de firewall automaticamente. Execute como administrador se necessario."
  }
}

$mutexName = "Global\LogisticaCasaDoCampoWatchdog"
$created = $false
$mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$created)
if (-not $created) {
  Write-WatchdogLog "Watchdog já em execução. Encerrando nova instância."
  exit 0
}

try {
  $pythonExe = Find-PythonExe
  if (-not $pythonExe) {
    Write-WatchdogLog "Python não encontrado. Instale Python 3.10+ e configure no PATH."
    exit 1
  }

  $env:APP_RUNTIME = "flask"
  $env:APP_HOST = $BindHost
  $env:APP_PORT = [string]$Port
  $env:LOGISTICA_HOST = $BindHost
  $env:LOGISTICA_PORT = [string]$Port
  $env:PYTHONUNBUFFERED = "1"
  $env:LOGISTICA_AUTOSTART = "1"
  if (-not $env:LOGISTICA_SESSION_MAX_AGE) {
    $env:LOGISTICA_SESSION_MAX_AGE = "28800"
  }

  Ensure-FirewallRule -PortToOpen $Port
  Write-WatchdogLog "Watchdog iniciado. Host=$BindHost Porta=$Port Python=$pythonExe"

  while ($true) {
    if (Test-PortListening -PortToCheck $Port) {
      Start-Sleep -Seconds 20
      continue
    }

    Write-WatchdogLog "Servidor parado. Iniciando run.py..."
    Push-Location $baseDir
    try {
      & $pythonExe run.py 1>> $runtimeLog 2>> $runtimeErrLog
      $exitCode = $LASTEXITCODE
    }
    finally {
      Pop-Location
    }

    Write-WatchdogLog "run.py encerrou com código $exitCode. Reinício em $RestartDelaySeconds s."
    Start-Sleep -Seconds $RestartDelaySeconds
  }
}
finally {
  if ($mutex) {
    try {
      $mutex.ReleaseMutex() | Out-Null
    }
    catch {
    }
    $mutex.Dispose()
  }
}
