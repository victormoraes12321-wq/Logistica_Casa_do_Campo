param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{2,62}$')][string]$TunnelName,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9.-]+$')][string]$Hostname,
    [ValidatePattern('^http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?$')][string]$Origin = 'http://127.0.0.1:3000',
    [switch]$InstallWindowsService
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$bundledExe = Join-Path $PSScriptRoot 'cloudflared.exe'
$foundCommand = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
$cloudflared = if (Test-Path -LiteralPath $bundledExe) { $bundledExe } elseif ($foundCommand) { $foundCommand.Source } else { $null }
if (-not $cloudflared) { throw 'cloudflared.exe não encontrado. Instale o binário oficial e execute novamente.' }
try {
    $health = Invoke-RestMethod -Uri "$($Origin.TrimEnd('/'))/healthz" -TimeoutSec 5
    if (-not $health.ok -or $health.status -ne 'ok' -or $health.service -ne 'logistica-casa-do-campo' -or $health.api_version -ne 'v1' -or $health.driver_api_version -ne 1) {
        throw 'healthz não corresponde à API do sistema'
    }
} catch { throw "O sistema local não está saudável em $Origin/healthz. Inicie-o antes de configurar o túnel." }
Write-Host '[1/7] Autenticando na conta Cloudflare (janela do navegador)...'
& $cloudflared tunnel login
if ($LASTEXITCODE -ne 0) { throw 'Login Cloudflare não concluído.' }
Write-Host '[2/7] Localizando ou criando túnel nomeado...'
$tunnels = @(& $cloudflared tunnel list --output json | ConvertFrom-Json)
$tunnel = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
if (-not $tunnel) {
    & $cloudflared tunnel create $TunnelName
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao criar túnel.' }
    $tunnels = @(& $cloudflared tunnel list --output json | ConvertFrom-Json)
    $tunnel = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
}
if (-not $tunnel -or -not $tunnel.id) { throw 'Não foi possível obter o UUID do túnel.' }
$tunnelId = [string]$tunnel.id
$userProfileDir = Join-Path $env:USERPROFILE '.cloudflared'
$sourceCredentials = Join-Path $userProfileDir "$tunnelId.json"
if (-not (Test-Path -LiteralPath $sourceCredentials)) { throw "Credencial não encontrada: $sourceCredentials" }
Write-Host '[3/7] Criando rota DNS estável...'
& $cloudflared tunnel route dns $tunnelId $Hostname
if ($LASTEXITCODE -ne 0) { throw 'Falha ao criar/validar rota DNS.' }
$runtimeDir = if ($InstallWindowsService) { 'C:\Windows\System32\config\systemprofile\.cloudflared' } else { $userProfileDir }
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$runtimeCredentials = Join-Path $runtimeDir "$tunnelId.json"
Copy-Item -LiteralPath $sourceCredentials -Destination $runtimeCredentials -Force
$configPath = Join-Path $runtimeDir 'config.yml'
$logPath = if ($InstallWindowsService) { 'C:\Cloudflared\cloudflared.log' } else { Join-Path $runtimeDir 'cloudflared.log' }
$config = @"
tunnel: $tunnelId
credentials-file: $runtimeCredentials

ingress:
  - hostname: $Hostname
    service: $Origin
    originRequest:
      connectTimeout: 10s
  - service: http_status:404

logfile: $logPath
loglevel: info
"@
Set-Content -LiteralPath $configPath -Value $config -Encoding UTF8
Write-Host '[4/7] Validando regras de ingresso...'
& $cloudflared tunnel --config $configPath ingress validate
if ($LASTEXITCODE -ne 0) { throw 'Configuração ingress inválida.' }
Write-Host '[5/7] Consultando informações do túnel...'
& $cloudflared tunnel info $tunnelId
if ($InstallWindowsService) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Abra o PowerShell como Administrador para instalar o serviço.' }
    Write-Host '[6/7] Instalando serviço Windows...'
    $binDir = 'C:\Cloudflared\bin'
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path $logPath) -Force | Out-Null
    $serviceExe = Join-Path $binDir 'cloudflared.exe'
    Copy-Item -LiteralPath $cloudflared -Destination $serviceExe -Force
    if (-not (Get-Service -Name cloudflared -ErrorAction SilentlyContinue)) {
        & $serviceExe service install
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar serviço cloudflared.' }
    }
    $imagePath = "`"$serviceExe`" --config=`"$configPath`" tunnel run"
    Set-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Services\cloudflared' -Name ImagePath -Value $imagePath
    Restart-Service -Name cloudflared
    Get-Service -Name cloudflared | Format-Table Status,Name,DisplayName
} else { Write-Host '[6/7] Serviço não solicitado. Use o lançador manual para teste.' }
Write-Host '[7/7] Configuração concluída.'
Write-Host "Origem pública: https://$Hostname"
Write-Host "Configuração: $configPath"
Write-Host "Valide: powershell -File `"$projectRoot\tools\check_driver_public_endpoint.ps1`" -PublicOrigin https://$Hostname"
