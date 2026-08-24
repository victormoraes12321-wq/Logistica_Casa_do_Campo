@echo off
title Cloudflare Tunnel - Acesso Externo Seguro Sem VPN
cd /d "%~dp0"

echo ======================================================================
echo   LOGISTICA CASA DO CAMPO - CLOUDFLARE TUNNEL (SEM VPN / SEM PORTAS)
echo ======================================================================
echo.
echo Este script cria um tunel HTTPS seguro e gratuito via Cloudflare.
echo Permite que motoristas no 4G/5G acessem o App do Motorista
echo sem precisar de VPN nem de abrir portas no roteador da empresa.
echo.

if not exist "cloudflared.exe" (
    echo [INFO] Baixando cloudflared.exe oficial da Cloudflare...
    curl.exe -L "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -o "cloudflared.exe"
)

echo.
echo [INICIANDO TUNEL HTTPS PARA A PORTA 3000...]
echo ----------------------------------------------------------------------
echo Aguarde alguns segundos ate aparecer o link HTTPS (.trycloudflare.com).
echo Envie esse link para os motoristas acessarem no celular!
echo ----------------------------------------------------------------------
echo.

cloudflared.exe tunnel --url http://127.0.0.1:3000

pause
