param(
  [string]$ServiceName = "LogisticaCasaDoCampo",
  [string]$DisplayName = "Logistica Casa do Campo",
  [string]$BindHost = "0.0.0.0",
  [int]$Port = 3000,
  [switch]$RemoveOnly
)

$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $PSScriptRoot
$logsDir = Join-Path $baseDir "logs"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

function Find-Python {
  $candidates = @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
  )
  foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source) { return $cmd.Source }
  throw "Python nao encontrado."
}

function Find-Nssm {
  $cmd = Get-Command nssm -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source) { return $cmd.Source }
  throw "NSSM nao encontrado no PATH. Instale NSSM ou use Task Scheduler (iniciar.bat opcao 4)."
}

$nssm = Find-Nssm

if ($RemoveOnly) {
  & $nssm stop $ServiceName | Out-Null
  & $nssm remove $ServiceName confirm | Out-Null
  Write-Host "Servico removido: $ServiceName"
  exit 0
}

$pythonExe = Find-Python
$runPy = Join-Path $baseDir "run.py"
$stdoutLog = Join-Path $logsDir "service_stdout.log"
$stderrLog = Join-Path $logsDir "service_stderr.log"

$env:APP_RUNTIME = "flask"
$env:APP_HOST = $BindHost
$env:APP_PORT = [string]$Port

& $nssm install $ServiceName $pythonExe $runPy | Out-Null
& $nssm set $ServiceName DisplayName $DisplayName | Out-Null
& $nssm set $ServiceName AppDirectory $baseDir | Out-Null
& $nssm set $ServiceName AppEnvironmentExtra "APP_RUNTIME=flask" "APP_HOST=$BindHost" "APP_PORT=$Port" | Out-Null
& $nssm set $ServiceName AppStdout $stdoutLog | Out-Null
& $nssm set $ServiceName AppStderr $stderrLog | Out-Null
& $nssm set $ServiceName AppRotateFiles 1 | Out-Null
& $nssm set $ServiceName AppRotateOnline 1 | Out-Null
& $nssm set $ServiceName AppRotateSeconds 86400 | Out-Null
& $nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $nssm set $ServiceName AppExit Default Restart | Out-Null
& $nssm start $ServiceName | Out-Null

Write-Host "Servico NSSM instalado e iniciado: $ServiceName"
