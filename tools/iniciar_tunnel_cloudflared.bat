@echo off
chcp 65001 >nul
title Cloudflare Tunnel - Acesso Externo Seguro Sem VPN
cls

echo ======================================================================
echo   LOGÍSTICA CASA DO CAMPO - CLOUDFLARE TUNNEL (SEM VPN / SEM ROUTER PORT)
echo ======================================================================
echo.
echo Este script cria um túnel HTTPS seguro e gratuito via Cloudflare.
echo Permite que motoristas no 4G/5G acessem o App do Motorista
echo sem precisar de VPN nem de abrir portas no roteador da empresa.
echo.

set "TOOL_DIR=%~dp0"
set "CLOUDFLARED_EXE=%TOOL_DIR%cloudflared.exe"

if exist "%CLOUDFLARED_EXE%" (
    echo [OK] Executável cloudflared.exe localizado em %CLOUDFLARED_EXE%
    goto START_TUNNEL
)

echo [INFO] Baixando cloudflared.exe oficial da Cloudflare...
powershell -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '%CLOUDFLARED_EXE%'"

if not exist "%CLOUDFLARED_EXE%" (
    echo [ERRO] Falha ao baixar cloudflared.exe. Verifique a conexão com a internet.
    pause
    exit /b 1
)

:START_TUNNEL
echo.
echo [INICIANDO TÚNEL HTTPS PARA O PORT 3000...]
echo ----------------------------------------------------------------------
echo Aguarde alguns segundos até aparecer o link HTTPS (.trycloudflare.com).
echo Envie esse link para os motoristas acessarem no celular!
echo.
"%CLOUDFLARED_EXE%" tunnel --url http://127.0.0.1:3000

pause
