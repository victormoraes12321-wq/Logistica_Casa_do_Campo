@echo off
title Cloudflare Tunnel - Acesso Externo Seguro Sem VPN
cls

echo ======================================================================
echo   LOGISTICA CASA DO CAMPO - CLOUDFLARE TUNNEL (SEM VPN / SEM PORTAS)
echo ======================================================================
echo.
echo Este script cria um tunel HTTPS seguro e gratuito via Cloudflare.
echo Permite que motoristas no 4G/5G acessem o App do Motorista
echo sem precisar de VPN nem de abrir portas no roteador da empresa.
echo.

set "TOOL_DIR=%~dp0"
set "CLOUDFLARED_EXE=%TOOL_DIR%cloudflared.exe"

if exist "%CLOUDFLARED_EXE%" (
    for %%I in ("%CLOUDFLARED_EXE%") do (
        if %%~zI GTR 30000000 (
            echo [OK] Executavel cloudflared.exe localizado e verificado (54MB).
            goto START_TUNNEL
        )
    )
    echo [AVISO] O arquivo cloudflared.exe anterior estava incompleto. Baixando novamente...
    del /f /q "%CLOUDFLARED_EXE%" >nul 2>&1
)

echo [INFO] Baixando cloudflared.exe oficial da Cloudflare (54MB)...
curl.exe -L "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -o "%CLOUDFLARED_EXE%"

if not exist "%CLOUDFLARED_EXE%" (
    echo [ERRO] Falha ao baixar cloudflared.exe. Verifique a conexao com a internet.
    pause
    exit /b 1
)

:START_TUNNEL
echo.
echo [INICIANDO TUNEL HTTPS PARA A PORTA 3000...]
echo ----------------------------------------------------------------------
echo Aguarde alguns segundos ate aparecer o link HTTPS (.trycloudflare.com).
echo Envie esse link para os motoristas acessarem no celular!
echo ----------------------------------------------------------------------
echo.
"%CLOUDFLARED_EXE%" tunnel --url http://127.0.0.1:3000

pause
