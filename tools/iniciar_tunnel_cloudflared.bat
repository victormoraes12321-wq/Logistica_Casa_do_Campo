@echo off
setlocal
title Logistica Casa do Campo - Cloudflare Tunnel Nomeado
set "LOCAL_EXE=%~dp0cloudflared.exe"
set "SERVICE_EXE=C:\Cloudflared\bin\cloudflared.exe"
set "DEFAULT_CONFIG=C:\Windows\System32\config\systemprofile\.cloudflared\config.yml"
set "CONFIG_FILE=%~1"
if "%CONFIG_FILE%"=="" set "CONFIG_FILE=%DEFAULT_CONFIG%"
if exist "%SERVICE_EXE%" set "CLOUDFLARED=%SERVICE_EXE%"
if not defined CLOUDFLARED if exist "%LOCAL_EXE%" set "CLOUDFLARED=%LOCAL_EXE%"
if not defined CLOUDFLARED (
  echo [ERRO] cloudflared.exe nao encontrado. Execute setup_cloudflared_named_tunnel.ps1.
  exit /b 2
)
if not exist "%CONFIG_FILE%" (
  echo [ERRO] Configuracao nomeada nao encontrada: %CONFIG_FILE%
  echo Consulte docs\cloudflare_named_tunnel_windows.md.
  exit /b 3
)
powershell -NoProfile -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:3000/healthz' -TimeoutSec 5; if(-not $r.ok -or $r.status -ne 'ok' -or $r.service -ne 'logistica-casa-do-campo' -or $r.api_version -ne 'v1' -or $r.driver_api_version -ne 1){exit 1} } catch { exit 1 }"
if errorlevel 1 (
  echo [ERRO] A API local nao respondeu em /healthz. Inicie o servidor primeiro.
  exit /b 4
)
echo [OK] API local saudavel. Validando e iniciando tunel nomeado...
"%CLOUDFLARED%" tunnel --config "%CONFIG_FILE%" ingress validate
if errorlevel 1 exit /b 5
"%CLOUDFLARED%" tunnel --config "%CONFIG_FILE%" run
exit /b %errorlevel%
