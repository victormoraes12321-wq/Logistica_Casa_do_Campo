param(
  [string]$Domain = "logistica",
  [string]$AltDomain = "logisticacasadocampo",
  [int]$AppPort = 3000,
  [string]$CaddyDir = "C:\Caddy"
)

$ErrorActionPreference = "Stop"

function Test-Admin {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($identity)
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
  Write-Host "[ERRO] Este script precisa ser executado como Administrador." -ForegroundColor Red
  exit 1
}

Write-Host "=======================================================================" -ForegroundColor Cyan
Write-Host "     CONFIGURACAO DO PROXY REVERSO (CADDY) PARA URLS LIMPAS" -ForegroundColor Cyan
Write-Host "=======================================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Criar diretorio Caddy
if (-not (Test-Path $CaddyDir)) {
  New-Item -ItemType Directory -Path $CaddyDir -Force | Out-Null
  Write-Host "[OK] Diretorio criado: $CaddyDir" -ForegroundColor Green
}

$caddyExe = Join-Path $CaddyDir "caddy.exe"
$caddyFile = Join-Path $CaddyDir "Caddyfile"

# 2. Download do Caddy.exe se nao existir
if (-not (Test-Path $caddyExe)) {
  Write-Host "[+] Baixando Caddy Web Server (versao oficial x64)..." -ForegroundColor Yellow
  $url = "https://caddyserver.com/api/download?os=windows&arch=amd64"
  $zipPath = Join-Path $env:TEMP "caddy.exe"
  try {
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $caddyExe -UseBasicParsing
    Write-Host "[OK] Caddy.exe instalado em: $caddyExe" -ForegroundColor Green
  } catch {
    Write-Host "[!] Nao foi possivel baixar o Caddy automaticamente da internet." -ForegroundColor Yellow
    Write-Host "    Baixe manualmente o caddy.exe de https://caddyserver.com/download e coloque em $CaddyDir" -ForegroundColor Yellow
  }
} else {
  Write-Host "[OK] Caddy.exe ja encontrado em: $caddyExe" -ForegroundColor Green
}

# 3. Gerar ou Atualizar Caddyfile
Write-Host "[+] Configurando arquivo de rotas (Caddyfile)..." -ForegroundColor Yellow

$caddyfileContent = @"
# Logistica Casa do Campo (Porta $AppPort)
http://$Domain, http://$AltDomain {
    reverse_proxy 127.0.0.1:$AppPort
}

# EXEMPLO PARA OS SEUS OUTROS SISTEMAS (Remova o # e ajuste a porta quando for adicionar):
# http://vendas {
#     reverse_proxy 127.0.0.1:5000
# }
# http://financeiro {
#     reverse_proxy 127.0.0.1:8080
# }
"@

Set-Content -Path $caddyFile -Value $caddyfileContent -Encoding UTF8
Write-Host "[OK] Caddyfile configurado em: $caddyFile" -ForegroundColor Green

# 4. Liberar Porta 80 no Firewall do Windows
Write-Host "[+] Liberando Porta 80 (HTTP padrao) no Firewall do Windows..." -ForegroundColor Yellow
try {
  $ruleName = "Caddy Proxy - Porta 80"
  $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
  if (-not $existing) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 80 -Profile Any | Out-Null
    Write-Host "[OK] Regra no Firewall criada para Porta 80." -ForegroundColor Green
  } else {
    Write-Host "[OK] Regra no Firewall ja existe para Porta 80." -ForegroundColor Green
  }
} catch {
  Write-Host "[!] Aviso: Nao foi possivel configurar o firewall automaticamente." -ForegroundColor Yellow
}

# 5. Agendar Caddy para iniciar com o Windows (Task Scheduler)
Write-Host "[+] Registrando Caddy para iniciar automaticamente no boot do Windows..." -ForegroundColor Yellow
$taskName = "CaddyReverseProxy"

try {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

if (Test-Path $caddyExe) {
  $action = New-ScheduledTaskAction -Execute $caddyExe -Argument "run --config `"$caddyFile`"" -WorkingDirectory $CaddyDir
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
  Write-Host "[OK] Tarefa agendada criada: $taskName" -ForegroundColor Green

  # Iniciar o Caddy agora mesmo
  Start-ScheduledTask -TaskName $taskName
  Write-Host "[OK] Servico Caddy iniciado com sucesso!" -ForegroundColor Green
}

Write-Host ""
Write-Host "=======================================================================" -ForegroundColor Cyan
Write-Host "         PROXY REVERSO CONFIGURADO COM SUCESSO!" -ForegroundColor Cyan
Write-Host "=======================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Acesso direto sem digitar a porta:" -ForegroundColor White
Write-Host "  - http://$Domain" -ForegroundColor Green
Write-Host "  - http://$AltDomain" -ForegroundColor Green
Write-Host ""
Write-Host "Para adicionar seus outros 2 sistemas no futuro:" -ForegroundColor White
Write-Host "  1. Abra o arquivo: $caddyFile" -ForegroundColor Yellow
Write-Host "  2. Adicione o novo nome e a porta do sistema." -ForegroundColor Yellow
Write-Host "  3. No prompt, rode: caddy reload (na pasta C:\Caddy)" -ForegroundColor Yellow
Write-Host ""
