# Implantação no Windows Server

Este repositório pode ser desenvolvido e compilado no PC principal, mas o backend de produção deve permanecer no Windows Server. O APK não precisa conhecer o IP do servidor: ele usa somente o hostname HTTPS permanente do Cloudflare Tunnel.

## Arquitetura recomendada

```text
Celular Android
    └── HTTPS: logistica.empresa.com.br
          └── Cloudflare Tunnel (serviço no Windows Server)
                └── http://127.0.0.1:3000
                      └── Waitress + aplicação Python (serviço NSSM)
                            └── banco de produção do servidor
```

Não abra a porta 3000 na internet. Quando `cloudflared` e o sistema estão na mesma máquina, mantenha o backend em `127.0.0.1:3000`; o túnel usa conexão de saída HTTPS e não precisa de redirecionamento de porta no roteador.

## 1. Preparar o servidor

- use uma pasta estável, por exemplo `C:\Apps\LogisticaCasaDoCampo`, fora de Desktop, Downloads e OneDrive;
- instale Python 3.12 x64, Git opcional, NSSM e o binário oficial `cloudflared`;
- permita saída HTTPS/443 para o Cloudflare;
- execute os comandos administrativos em PowerShell elevado somente quando necessário.

Crie o ambiente da aplicação:

```powershell
Set-Location C:\Apps\LogisticaCasaDoCampo
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. Configuração exclusiva do servidor

Crie `C:\Apps\LogisticaCasaDoCampo\.env` a partir de `.env.example`. Esse arquivo não deve voltar para o PC, Git, APK ou ChatGPT.

Configuração mínima para o túnel na mesma máquina:

```dotenv
FLASK_ENV=production
DEBUG=false
APP_RUNTIME=flask
APP_HOST=127.0.0.1
APP_PORT=3000
LOGISTICA_HOST=127.0.0.1
LOGISTICA_PORT=3000
LOGISTICA_ALLOWED_HOSTS=logistica.empresa.com.br,localhost,127.0.0.1
LOGISTICA_SECURE_COOKIE=1
LOGISTICA_ALLOW_PROD_DEBUG=0
LOGISTICA_ALLOW_EPHEMERAL_SECRET=0
SECRET_KEY=VALOR_LONGO_ALEATORIO_E_PERMANENTE
DATABASE_URL=sqlite:///data/logistica_casa_do_campo.sqlite3
```

Gere `SECRET_KEY` uma vez, guarde-a com segurança e não a altere a cada atualização. Se a operação usar PostgreSQL, substitua `DATABASE_URL` pela conexão de produção. Credenciais do ERP devem existir somente nesse `.env` e o usuário do ERP deve ser somente leitura.

## 3. Banco e validação antes do serviço

Faça backup antes de migração ou atualização:

```powershell
.\.venv\Scripts\python.exe tools\backup_automation.py --mode backup --keep-max 7
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe tools\regression_release_gate.py
```

Não substitua o banco de produção por uma cópia do PC principal. Em atualizações, preserve `.env`, `data`, `backups` e `logs` do servidor.

## 4. Instalar o backend como serviço

O instalador prefere automaticamente `.venv\Scripts\python.exe` e usa `127.0.0.1` por padrão:

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_nssm_service.ps1 `
  -ServiceName LogisticaCasaDoCampo `
  -BindHost 127.0.0.1 `
  -Port 3000
```

Valide:

```powershell
Get-Service LogisticaCasaDoCampo
Invoke-RestMethod http://127.0.0.1:3000/healthz
Get-Content logs\service_stderr.log -Tail 100
```

O processo usa Waitress. Se Flask, Waitress ou dotenv não estiverem instalados no Python selecionado, o instalador interrompe antes de criar um serviço aparentemente saudável.

## 5. Cloudflare Tunnel permanente

Com o backend local saudável:

```powershell
powershell -ExecutionPolicy Bypass -File tools\setup_cloudflared_named_tunnel.ps1 `
  -TunnelName logistica-casa-do-campo `
  -Hostname logistica.empresa.com.br `
  -Origin http://127.0.0.1:3000 `
  -InstallWindowsService
```

Depois valide de uma conexão externa:

```powershell
Get-Service cloudflared
powershell -File tools\check_driver_public_endpoint.ps1 `
  -PublicOrigin https://logistica.empresa.com.br
```

Configure o app Android com `https://logistica.empresa.com.br`, nunca com IP local, `localhost` ou URL `trycloudflare.com`.

## 6. Atualizações vindas do PC principal

1. Faça backup do banco no servidor.
2. Pare `LogisticaCasaDoCampo`; não é necessário remover o serviço.
3. Atualize somente código, templates, arquivos estáticos, migrations, scripts e requirements.
4. Preserve `.env`, banco, logs, backups e credenciais do Cloudflare.
5. Atualize dependências e execute `alembic upgrade head`.
6. Rode o gate técnico e inicie novamente o serviço.
7. Valide `/healthz` local e público; depois homologue o Android.

```powershell
Stop-Service LogisticaCasaDoCampo
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe tools\regression_release_gate.py
Start-Service LogisticaCasaDoCampo
Invoke-RestMethod http://127.0.0.1:3000/healthz
```

## 7. Pós-reinicialização e operação

Após reiniciar o Windows Server:

```powershell
Get-Service LogisticaCasaDoCampo,cloudflared
powershell -ExecutionPolicy Bypass -File tools\run_checklists.ps1 -Mode post-reboot
```

Confirme diariamente `/healthz`, login administrativo, logs sem erro crítico e backup recente verificado. O roteiro operacional complementar está em `docs\runbook_operacao_windows.md`.
