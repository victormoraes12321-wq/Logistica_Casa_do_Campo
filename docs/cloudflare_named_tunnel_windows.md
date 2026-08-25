# Cloudflare Tunnel nomeado no Windows

O aplicativo Android precisa de uma origem HTTPS estável. Quick Tunnels (`*.trycloudflare.com`) mudam de endereço e não devem ser usados na operação diária.

## Pré-requisitos

- domínio ativo sob a conta Cloudflare da empresa;
- permissão para criar Tunnel e registro DNS;
- sistema respondendo localmente em `http://127.0.0.1:3000/healthz`;
- PowerShell como Administrador somente para instalar o serviço.

Nenhum token, `cert.pem` ou arquivo `<UUID>.json` deve ser versionado. O script usa o login interativo oficial e armazena a credencial no perfil de execução do serviço.

## Configuração

```powershell
powershell -ExecutionPolicy Bypass -File tools\setup_cloudflared_named_tunnel.ps1 `
  -TunnelName logistica-casa-do-campo `
  -Hostname logistica.seudominio.com.br `
  -InstallWindowsService
```

O script valida `/healthz`, abre o login Cloudflare, reutiliza ou cria o túnel nomeado, cria a rota DNS, gera e valida `config.yml`, copia a credencial ao perfil `LocalSystem` e instala/reinicia o serviço. Sem `-InstallWindowsService`, a configuração fica em `%USERPROFILE%\.cloudflared` para teste manual. O exemplo está em `deploy/cloudflared/config.example.yml`.

## Validação

```powershell
Get-Service cloudflared
powershell -File tools\check_driver_public_endpoint.ps1 `
  -PublicOrigin https://logistica.seudominio.com.br
```

O healthcheck deve informar `ok=true`, `status=ok`, `service=logistica-casa-do-campo`, `api_version=v1`, `driver_api_version=1` e a versão do sistema; não expõe host interno, porta ou credenciais.

## Diagnóstico

```powershell
Get-Service cloudflared
Get-Content C:\Cloudflared\cloudflared.log -Tail 100
Restart-Service cloudflared
```

Se o público falhar, teste primeiro `http://127.0.0.1:3000/healthz`. Se o local falhar, corrija sistema/porta 3000; se apenas o público falhar, examine serviço, DNS e log.

Referências oficiais: [criar túnel gerenciado localmente](https://developers.cloudflare.com/tunnel/advanced/local-management/create-local-tunnel/) e [executar como serviço no Windows](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/as-a-service/windows/).
