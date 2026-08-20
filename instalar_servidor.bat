@echo off
setlocal enabledelayedexpansion
title Instalacao Automatica - Logistica Casa do Campo (Servidor)

echo.
echo =======================================================================
echo          LOGISTICA CASA DO CAMPO - INSTALACAO DE SERVIDOR
echo =======================================================================
echo.

:: 1. Verificar Privilegios de Administrador
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Este script precisa ser executado como ADMINISTRADOR.
    echo [!] Clique com o botao direito em 'instalar_servidor.bat' e selecione 'Executar como Administrador'.
    echo.
    pause
    exit /b 1
)

cd /d "%~dp0"

echo [+] Pasta do sistema: %CD%
echo.

:: 2. Verificar Instalacao do Python
echo [1/6] Verificando instalacao do Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Python nao foi encontrado no PATH do sistema.
    echo.
    echo Por favor, instale o Python 3.10 ou superior no servidor.
    echo ATENCAO: Marque a opcao "Add Python to PATH" durante a instalacao!
    echo Download: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version') do set PYTHON_VER=%%v
echo [OK] %PYTHON_VER% detectado com sucesso.
echo.

:: 3. Criar Diretorios e Configuracao de Ambiente (.env)
echo [2/6] Preparando diretorios e arquivos de ambiente...
if not exist "data" mkdir data
if not exist "backups" mkdir backups
if not exist "logs" mkdir logs

if not exist ".env" (
    echo [+] Criando arquivo .env de producao...
    python -c "import secrets; f=open('.env','w'); f.write('FLASK_ENV=production\nDEBUG=false\nSECRET_KEY=' + secrets.token_hex(32) + '\nAPP_RUNTIME=flask\nAPP_HOST=0.0.0.0\nAPP_PORT=3000\nLOGISTICA_ALLOWED_HOSTS=*\nLOGISTICA_LEGACY_PROXY_PORT=4000\nDATABASE_URL=sqlite:///data/logistica_casa_do_campo.sqlite3\nLOGISTICA_SECURE_COOKIE=0\n'); f.close()"
    echo [OK] Arquivo .env gerado com chave de seguranca unica.
) else (
    echo [OK] Arquivo .env ja existente preservado.
)
echo.

:: 4. Instalar Dependencias do Python
echo [3/6] Instalando dependencias de producao...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERRO] Falha ao instalar dependencias via pip. Verifique a conexao com a internet.
    pause
    exit /b 1
)
echo [OK] Dependencias instaladas com sucesso.
echo.

:: 5. Executar Migracao do Banco de Dados
echo [4/6] Aplicando estrutura do banco de dados (Alembic)...
python -m alembic upgrade head >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Aplicando bootstrap no banco existente...
    python tools/alembic_bootstrap.py
)
echo [OK] Banco de dados pronto.
echo.

:: 6. Configurar Firewall do Windows (Porta 3000)
echo [5/6] Configurando Firewall do Windows para liberar a porta 3000...
netsh advfirewall firewall show rule name="Logistica Casa do Campo - Porta 3000" >nul 2>&1
if %errorlevel% neq 0 (
    netsh advfirewall firewall add rule name="Logistica Casa do Campo - Porta 3000" dir=in action=allow protocol=TCP localport=3000 profile=any >nul 2>&1
    echo [OK] Regra de entrada criada no Firewall (Porta 3000 Liberada).
) else (
    echo [OK] Regra no Firewall ja existe.
)
echo.

:: 7. Configurar Inicializacao Automatica e Backup Diario (Windows Tasks)
echo [6/6] Configurando Inicializacao Automatica no boot e Backups Diarios...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install_windows_tasks.ps1" -Force
if %errorlevel% eq 0 (
    echo [OK] Tarefas do Windows instaladas! O sistema iniciara sozinho no boot.
) else (
    echo [AVISO] Nao foi possivel registrar as tarefas agendadas automaticamente.
)
echo.

:: 8. Obter IP da Rede Local
set "SERVER_IP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    set "IP_TRIM=%%a"
    set "IP_TRIM=!IP_TRIM:~1!"
    if not "!IP_TRIM!"=="127.0.0.1" (
        if "!SERVER_IP!"=="" set "SERVER_IP=!IP_TRIM!"
    )
)

echo =======================================================================
echo         INSTALACAO CONCLUIDA COM SUCESSO NO SERVIDOR!
echo =======================================================================
echo.
echo URLs de Acesso ao Sistema:
echo   - Neste Servidor:  http://localhost:3000
if not "!SERVER_IP!"=="" (
echo   - Computadores da Rede Local: http://!SERVER_IP!:3000
) else (
echo   - Computadores da Rede Local: http://[IP_DO_SERVIDOR]:3000
)
echo.
echo Para iniciar o servidor manualmente agora: execute iniciar.bat
echo O sistema ja esta configurado para ligar junto com o Windows!
echo.
pause
endlocal
