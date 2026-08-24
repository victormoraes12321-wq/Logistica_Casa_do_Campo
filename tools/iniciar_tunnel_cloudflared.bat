@echo off
title Logistica Casa do Campo - Tunel HTTPS Seguro (Keep-Alive 24/7)
cd /d "%~dp0"

echo ======================================================================
echo   LOGISTICA CASA DO CAMPO - TUNEL HTTPS ESTAVEL 24/7 (SEM QUEDA)
echo ======================================================================
echo.
echo Este script cria o link HTTPS seguro para os motoristas no 4G/5G/Wi-Fi.
echo Inclui Keep-Alive ativo para EVITAR quedas e o erro "no tunnel here".
echo.
echo Escolha o metodo de conexao:
echo.
echo [1] SSH Tunel Estavel (RECOMENDADO - Keep-Alive 15s sem queda)
echo [2] Cloudflare Tunel
echo.
set /p MOP="Digite a opcao desejada (1 ou 2) e pressione ENTER [Padrao: 1]: "

if "%MOP%"=="2" goto CLOUDFLARE

:SSH
echo.
echo [INICIANDO TUNEL SSH ESTAVEL COM KEEP-ALIVE 24/7...]
echo ----------------------------------------------------------------------
echo Aguarde a criacao do link HTTPS e do QR Code...
echo Aponte a camera do aplicativo do motorista para o QR Code abaixo!
echo ----------------------------------------------------------------------
echo.
ssh.exe -o StrictHostKeyChecking=no -o ServerAliveInterval=15 -o ServerAliveCountMax=99999 -R 80:127.0.0.1:3000 nokey@localhost.run
goto FIM

:CLOUDFLARE
if not exist "cloudflared.exe" (
    echo [INFO] Baixando cloudflared.exe oficial...
    curl.exe -L "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -o "cloudflared.exe"
)
echo.
echo [INICIANDO CLOUDFLARE TUNEL NA PORTA 3000...]
echo ----------------------------------------------------------------------
echo Aguarde a criacao do link HTTPS (.trycloudflare.com)...
echo ----------------------------------------------------------------------
echo.
cloudflared.exe tunnel --url http://127.0.0.1:3000 --protocol http2 --edge-ip-version 4 --keep-alive-interval 15s

:FIM
pause
