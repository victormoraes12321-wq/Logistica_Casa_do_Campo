# Inicia o tunel rapido do Cloudflare para acesso externo seguro (HTTPS) do App do Motorista
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$cloudflared = Join-Path $PSScriptRoot 'cloudflared.exe'
$envFile = Join-Path $projectRoot '.env'

if (-not (Test-Path $cloudflared)) {
    Write-Host "[ERRO] cloudflared.exe nao encontrado em $cloudflared" -ForegroundColor Red
    exit 1
}

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "    LOGISTICA CASA DO CAMPO - TUNEL DE ACESSO REMOTO    " -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

# 1. Verificar se a API local esta respondendo
Write-Host "`n[1/3] Verificando se o servidor local esta ativo na porta 3000..." -ForegroundColor Yellow
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:3000/healthz" -TimeoutSec 5
    if ($health.ok -eq $true -or $health.status -eq 'ok') {
        Write-Host "  -> Servidor local OK! Versao: $($health.system_version)" -ForegroundColor Green
    }
} catch {
    Write-Host "[ALERTA] O servidor na porta 3000 nao respondeu ao /healthz." -ForegroundColor Red
    Write-Host "Certifique-se de que o run.py ou o servico de Watchdog esta ativo." -ForegroundColor Red
}

# 2. Iniciar Cloudflared Quick Tunnel
Write-Host "`n[2/3] Criando tunel HTTPS seguro gratuito via Cloudflare..." -ForegroundColor Yellow
$logFile = Join-Path $env:TEMP "cloudflared_quick.log"
if (Test-Path $logFile) { Remove-Item $logFile -Force }

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $cloudflared
$psi.Arguments = "tunnel --url http://127.0.0.1:3000"
$psi.UseShellExecute = $false
$psi.RedirectStandardError = $true
$psi.RedirectStandardOutput = $true
$psi.CreateNoWindow = $true

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
$proc.Start() | Out-Null

$tunnelUrl = $null
$maxWait = 30
$waited = 0

while (-not $tunnelUrl -and $waited -lt $maxWait) {
    Start-Sleep -Seconds 1
    $waited++
    while (-not $proc.StandardError.EndOfStream) {
        $line = $proc.StandardError.ReadLine()
        Add-Content -Path $logFile -Value $line
        if ($line -match "https://[a-zA-Z0-9-]+\.trycloudflare\.com") {
            $tunnelUrl = $matches[0]
            break
        }
    }
}

if (-not $tunnelUrl) {
    Write-Host "[ERRO] Nao foi possivel obter a URL publica do tunel em $maxWait segundos." -ForegroundColor Red
    if (Test-Path $logFile) { Get-Content $logFile -Tail 15 }
    $proc.Kill()
    exit 1
}

$tunnelHost = ([System.Uri]$tunnelUrl).Host
Write-Host "  -> Tunel criado com sucesso: $tunnelUrl" -ForegroundColor Green

# 3. Atualizar LOGISTICA_ALLOWED_HOSTS no .env
Write-Host "`n[3/3] Atualizando LOGISTICA_ALLOWED_HOSTS no .env..." -ForegroundColor Yellow
if (Test-Path $envFile) {
    $envContent = Get-Content $envFile -Raw
    if ($envContent -match "LOGISTICA_ALLOWED_HOSTS=([^\r\n]*)") {
        $currentHosts = $matches[1].Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
        if ($currentHosts -notcontains $tunnelHost) {
            $newHosts = ($currentHosts + $tunnelHost) -join ','
            $envContent = $envContent -replace "LOGISTICA_ALLOWED_HOSTS=([^\r\n]*)", "LOGISTICA_ALLOWED_HOSTS=$newHosts"
            Set-Content -Path $envFile -Value $envContent -NoNewline
            Write-Host "  -> Host '$tunnelHost' adicionado aos hosts permitidos!" -ForegroundColor Green
        } else {
            Write-Host "  -> Host '$tunnelHost' ja estava autorizado." -ForegroundColor Green
        }
    }
}

Write-Host "`n========================================================" -ForegroundColor Green
Write-Host "        CONEXAO PRONTA PARA O APLICATIVO DO MOTORISTA   " -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host "URL Base do Servidor : $tunnelUrl" -ForegroundColor Cyan
Write-Host "App Web do Motorista : $tunnelUrl/static/driver_app/index.html" -ForegroundColor Cyan
Write-Host "Painel Administrativo: $tunnelUrl/dashboard" -ForegroundColor Cyan
Write-Host "Mapa de Entregas     : $tunnelUrl/map" -ForegroundColor Cyan
Write-Host "`nNo App Android, configure a URL do servidor como:" -ForegroundColor White
Write-Host "  $tunnelUrl" -ForegroundColor Yellow
Write-Host "`nMantenha esta janela aberta para manter o tunel ativo." -ForegroundColor Gray
Write-Host "Pressione Ctrl+C para encerrar o tunel." -ForegroundColor Gray
Write-Host "========================================================`n" -ForegroundColor Green

try {
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds 2
    }
} finally {
    if (-not $proc.HasExited) {
        $proc.Kill()
    }
}
