import base64

ps_code = r"""
$ErrorActionPreference = 'Stop'
$d = 'C:\\Caddy'
if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
$e = "$d\\caddy.exe"
$f = "$d\\Caddyfile"
if (-not (Test-Path $e)) {
    Write-Host '[+] Baixando Caddy Web Server (versao oficial x64)...' -ForegroundColor Yellow
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri 'https://caddyserver.com/api/download?os=windows&arch=amd64' -OutFile $e -UseBasicParsing
    Write-Host '[OK] Caddy.exe instalado com sucesso!' -ForegroundColor Green
} else {
    Write-Host '[OK] Caddy.exe encontrado em C:\Caddy' -ForegroundColor Green
}

$c = "http://logistica, http://logisticacasadocampo {`n    reverse_proxy 127.0.0.1:3000`n}"
Set-Content -Path $f -Value $c -Encoding UTF8
Write-Host '[OK] Caddyfile configurado para http://logistica' -ForegroundColor Green

try {
    netsh advfirewall firewall add rule name="Caddy Proxy - Porta 80" dir=in action=allow protocol=TCP localport=80 profile=any | Out-Null
    Write-Host '[OK] Regra de Firewall criada na Porta 80.' -ForegroundColor Green
} catch {}

$a = New-ScheduledTaskAction -Execute $e -Argument 'run --config "C:\Caddy\Caddyfile"' -WorkingDirectory $d
$t = New-ScheduledTaskTrigger -AtStartup
$p = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

try { Unregister-ScheduledTask -TaskName 'CaddyReverseProxy' -Confirm:$false -ErrorAction SilentlyContinue } catch {}
Register-ScheduledTask -TaskName 'CaddyReverseProxy' -Action $a -Trigger $t -Principal $p -Settings $s | Out-Null
try { Start-ScheduledTask -TaskName 'CaddyReverseProxy' } catch {}

Write-Host ""
Write-Host "=======================================================================" -ForegroundColor Cyan
Write-Host "         PROXY REVERSO CONFIGURADO COM SUCESSO NO SERVIDOR!" -ForegroundColor Cyan
Write-Host "=======================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Acesse o sistema sem digitar a porta:" -ForegroundColor White
Write-Host "  - http://logistica" -ForegroundColor Green
Write-Host "  - http://logisticacasadocampo" -ForegroundColor Green
Write-Host ""
"""

b64 = base64.b64encode(ps_code.encode('utf-16-le')).decode('ascii')
print("B64 LENGTH:", len(b64))

bat_code = f"""@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Configurar Proxy Caddy - URLs Limpas

echo.
echo =======================================================================
echo     CONFIGURANDO PROXY REVERSO (CADDY) PARA ENDERECOS LIMPOS
echo =======================================================================
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Este script precisa ser executado como ADMINISTRADOR.
    echo [!] Clique com o botao direito em 'configurar_proxy.bat' e escolha 'Executar como Administrador'.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {b64}

echo.
pause
endlocal
"""

open("configurar_proxy.bat", "w", encoding="utf-8").write(bat_code)
print("configurar_proxy.bat gerado com sucesso!")
