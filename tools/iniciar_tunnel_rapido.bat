@echo off
setlocal
title Logistica Casa do Campo - Tunel HTTPS do Motorista
cd /d "%~dp0"
echo ========================================================
echo   Iniciando Tunel Seguro HTTPS (Cloudflare Quick Tunnel)
echo ========================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar_tunnel_rapido.ps1"
if %errorlevel% neq 0 (
    echo.
    echo [ERRO] Falha ao executar o tunel. Pressione qualquer tecla para sair.
    pause >nul
)
