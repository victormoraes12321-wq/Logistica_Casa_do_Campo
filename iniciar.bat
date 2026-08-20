@echo off
title Logistica Casa do Campo - Servidor 24/7
setlocal enabledelayedexpansion
cd /d "%~dp0"
if not exist logs mkdir logs
if not exist data mkdir data
if not exist backups mkdir backups

:: 1. Busca inteligente por um executavel Python funcional (testa import sys)
set "PYTHON_EXE="

if defined LOGISTICA_PYTHON_EXE (
    "%LOGISTICA_PYTHON_EXE%" -c "import sys, socket" >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=%LOGISTICA_PYTHON_EXE%"
)

if "!PYTHON_EXE!"=="" (
    for %%P in (
        "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "C:\Python314\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "C:\Python310\python.exe"
    ) do (
        if "!PYTHON_EXE!"=="" if exist "%%~fP" (
            "%%~fP" -c "import sys, socket" >nul 2>&1
            if not errorlevel 1 set "PYTHON_EXE=%%~fP"
        )
    )
)

if "!PYTHON_EXE!"=="" (
    py -3 -c "import sys, socket" >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=py -3"
)

if "!PYTHON_EXE!"=="" (
    python -c "import sys, socket" >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=python"
)

if "!PYTHON_EXE!"=="" (
    echo.
    echo =======================================================================
    echo [ERRO CRITICO] Nenhum ambiente Python valido foi encontrado!
    echo =======================================================================
    echo Por favor, instale o Python 3.10 ou superior no servidor.
    echo Certifique-se de marcar a opcao "Add Python to PATH" ao instalar.
    echo Download: https://www.python.org/downloads/
    echo =======================================================================
    echo.
    pause
    exit /b 1
)

:: 2. Auto-verificacao de dependencias essenciais (dotenv, flask, waitress)
"!PYTHON_EXE!" -c "import dotenv, flask, waitress" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo =======================================================================
    echo    INSTALANDO DEPENDENCIAS DO SISTEMA (PRIMEIRA EXECUCAO NO SERVIDOR)
    echo =======================================================================
    echo.
    "!PYTHON_EXE!" -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo.
        echo [ERRO] Falha ao instalar dependencias do Python.
        echo Verifique se o servidor esta conectado a internet.
        echo.
        pause
        exit /b 1
    )
    echo [OK] Dependencias instaladas com sucesso!
    echo.
)

:: Atalhos de argumento direto
if /I "%~1"=="auto" goto :mode_network
if /I "%~1"=="watchdog" goto :mode_network
if /I "%~1"=="network" goto :mode_network
if /I "%~1"=="local" goto :mode_local

echo.
echo =======================================================================
echo           LOGISTICA CASA DO CAMPO - INICIALIZACAO DE SERVIDOR
echo =======================================================================
echo Executavel Python detectado: !PYTHON_EXE!
echo.
echo Escolha o modo de execucao:
echo [1] Servidor 24/7 - Acesso pela Rede (Recomendado para Servidor)
echo [2] Servidor 24/7 - Acesso somente Local (127.0.0.1)
echo [3] Instalar Tarefas Agendadas no Windows (Startup + Backup Diario)
echo.
choice /C 123 /N /T 8 /D 1 /M "Digite 1, 2 ou 3 (padrao: 1 em 8s): "
if errorlevel 3 goto :install_tasks
if errorlevel 2 goto :mode_local
if errorlevel 1 goto :mode_network

:mode_network
echo Starting Logistica Casa do Campo (Rede 0.0.0.0:3000)...
"!PYTHON_EXE!" "%~dp0tools\server_monitor.py" --host 0.0.0.0 --port 3000
goto :end

:mode_local
echo Starting Logistica Casa do Campo (Local 127.0.0.1:3000)...
"!PYTHON_EXE!" "%~dp0tools\server_monitor.py" --host 127.0.0.1 --port 3000
goto :end

:install_tasks
echo Instalando tarefas agendadas do Windows...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install_windows_tasks.ps1" -Force
echo.
pause
goto :end

:end
endlocal
