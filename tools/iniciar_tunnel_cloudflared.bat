@echo off
title Logistica Casa do Campo - Tunel HTTPS Seguro (Sem VPN)
cd /d "%~dp0"

echo ======================================================================
echo   LOGISTICA CASA DO CAMPO - TUNEL HTTPS SEGURO (SEM VPN)
echo ======================================================================
echo.
echo Este script conecta o App do Motorista (4G/5G/Wi-Fi) ao computador
echo da empresa de forma segura e sem precisar abrir portas no roteador.
echo.
echo Escolha o metodo de conexao:
echo.
echo [1] SSH Tunel Instantaneo (RECOMENDADO - Funciona em qualquer firewall)
echo [2] Cloudflare Tunel
echo.
set /p MOP="Digite a opcao desejada (1 ou 2) e pressione ENTER [Padrao: 1]: "

if "%MOP%"=="2" goto CLOUDFLARE

:SSH
echo.
echo [INICIANDO TUNEL SSH SEGURO NA PORTA 3000...]
echo ----------------------------------------------------------------------
echo Aguarde a criacao do link HTTPS...
echo Envie o link HTTPS (.lhr.life) gerado para os motoristas no celular!
echo ----------------------------------------------------------------------
echo.
ssh.exe -o StrictHostKeyChecking=no -R 80:127.0.0.1:3000 nokey@localhost.run
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
cloudflared.exe tunnel --url http://127.0.0.1:3000 --protocol http2 --edge-ip-version 4

:FIM
pause
