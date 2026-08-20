param(
  [string]$Alias = "logisticacasadocampo",
  [string]$ServerIp = "",
  [switch]$Remove,
  [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-Admin {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($identity)
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-LocalIPv4 {
  try {
    $udp = New-Object System.Net.Sockets.UdpClient
    $udp.Client.Connect("8.8.8.8", 80)
    $ip = $udp.Client.LocalEndPoint.Address.ToString()
    $udp.Close()
    if ($ip -and $ip -ne "127.0.0.1") {
      return $ip
    }
  } catch {}

  $candidate = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
      $_.IPAddress -and
      $_.IPAddress -ne "127.0.0.1" -and
      $_.IPAddress -notlike "169.254.*" -and
      $_.PrefixOrigin -in @("Dhcp", "Manual")
    } |
    Select-Object -First 1
  if ($candidate) {
    return $candidate.IPAddress
  }
  throw "Nao foi possivel identificar o IPv4 local automaticamente. Use -ServerIp."
}

if (-not $DryRun -and -not (Test-Admin)) {
  throw "Execute este script como Administrador."
}

$aliasName = ($Alias | ForEach-Object { "$_".Trim().ToLowerInvariant() })
if (-not $aliasName) {
  throw "Alias invalido."
}

$hostsPath = Join-Path $env:windir "System32\drivers\etc\hosts"
if (-not (Test-Path $hostsPath)) {
  throw "Arquivo hosts nao encontrado: $hostsPath"
}

$lines = Get-Content -Path $hostsPath -ErrorAction Stop
$escaped = [Regex]::Escape($aliasName)
$filtered = @()
foreach ($line in $lines) {
  if ($line -match "^\s*#") {
    $filtered += $line
    continue
  }
  if ($line -match "(^|\s)$escaped(\s|$)") {
    continue
  }
  $filtered += $line
}

if (-not $Remove) {
  if (-not $ServerIp) {
    $ServerIp = Get-LocalIPv4
  }
  $ServerIp = "$ServerIp".Trim()
  if (-not ($ServerIp -match "^\d{1,3}(\.\d{1,3}){3}$")) {
    throw "ServerIp invalido: $ServerIp"
  }
  $filtered += "$ServerIp`t$aliasName"
}

if ($DryRun) {
  Write-Host "[DRY-RUN] Nenhuma alteracao aplicada em $hostsPath"
} else {
  Set-Content -Path $hostsPath -Value $filtered -Encoding ASCII
  ipconfig /flushdns | Out-Null
}

if ($Remove) {
  Write-Host "Alias removido: $aliasName"
} else {
  Write-Host "Alias configurado: $aliasName -> $ServerIp"
  Write-Host "Acesso sugerido: http://$aliasName`:3000"
}
