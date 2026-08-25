# Relatório final — App Motorista v2.7.0

Data do gate: 25/08/2026  
Versão do sistema: `v2.7.0-20260825`

## Diagnóstico de origem

- login do motorista aceitava o nome sem validar o PIN;
- senha podia ser gravada em texto puro e a própria API executava `ALTER TABLE`;
- endpoints de rotas, saída, detalhe e entrega não exigiam sessão;
- o filtro por nome podia retornar todas as cargas quando não encontrava correspondência;
- registro de problema usava `route_id` e `notes`, mas o banco possuía `description` e não possuía `route_id`, causando HTTP 500;
- entrega atualizava todos os vínculos do mesmo pedido e não possuía chave idempotente;
- PWA guardava nome/PIN, mantinha uma segunda URL de servidor, dependia de CDN bloqueada pela CSP e usava `alert`/fila `localStorage`;
- Android retornava URI `file:` diferente do `content://` entregue à câmera, concedia permissões WebView irrestritas e pedia áudio/armazenamento;
- havia duas árvores de fonte Android divergentes e faltavam scripts/JAR do Gradle Wrapper;
- o túnel existente era aleatório e temporário.

## Backend e banco

- senha de motorista em PBKDF2-SHA256 (260.000 iterações e salt individual);
- senha temporária `123`, `must_change_password=1` e troca obrigatória para valor não vazio;
- credenciais legadas `pin` são zeradas após backfill;
- bearer token aleatório com persistência somente de SHA-256, expiração configurável, `last_seen_at`, revogação e logout;
- cadastro público pelo app desativado; novos motoristas criados no painel recebem hash de `123` e troca obrigatória;
- lista pública contém somente `id` e `name`; a UI mostra somente o nome e usa `driver_id` como identidade;
- rotas, detalhe, saída e entrega usam exclusivamente o motorista da sessão;
- entrega/problema usa `BEGIN IMMEDIATE`, valida carga/pedido/motorista/estado e altera o vínculo exato `(route_id, order_id)`;
- `idempotency_key` única com fingerprint da requisição e replay seguro da resposta;
- comprovante e assinatura têm validação Base64 e limites de 8 MB/2 MB;
- carga encerra apenas quando todas as paradas estão terminais: `Acertada` sem ocorrência, `Com problema` se houver problema;
- `/healthz` retorna `ok`, `status`, `api_version` e `system_version` sem host/porta internos;
- histórico de pedidos passou a mostrar finalizações recentes primeiro.

Migração aplicada: `0004_driver_app_security_integrity`. A base ativa ficou em revisão `0004`; 8 motoristas existentes receberam hash e troca obrigatória. Nenhum pedido ou rota foi modificado pela migração. O Alembic foi corrigido para recarregar a `DATABASE_URL` e descartar o engine no Windows.

## PWA e offline

- única origem: `window.location.origin`;
- nenhuma senha é armazenada; a sessão local contém token/expiração/identidade;
- dashboard de cargas, progresso, paradas, peso e estados vazios/erro/carregamento;
- modais próprios e toasts, sem `alert()`/`confirm()` genéricos;
- foto com compressão, qualidade aproximada, prévia e remoção;
- assinatura em modal realmente fullscreen, responsiva em retrato/paisagem e preservada ao redimensionar;
- QR removido do PWA; configuração fica exclusivamente no Android nativo;
- fila IndexedDB com `id`, `idempotency_key`, `order_id`, `route_id`, `created_at`, `attempts`, `next_retry_at`, `status`, `last_error` e `payload`;
- backoff exponencial de 30 s até 30 min, classificação de erro retryable/permanente e contadores pendentes/enviadas/falhas;
- Service Worker v2.7.0 guarda somente o app shell; API nunca é cacheada.

## Android

- origem nativa normalizada e salva uma única vez em `server_origin`;
- HTTPS obrigatório para host público; HTTP aceito apenas para endereço local/privado;
- `/healthz` com timeouts e três tentativas antes de oferecer tentar novamente, trocar ou continuar offline;
- WebView aceita navegação interna somente na origem salva; telefone/SMS/geo/market/HTTP(S) externos usam Intent;
- acesso a arquivo e universal-from-file desativados; WebRTC negado; mixed content bloqueado;
- câmera preserva e retorna o mesmo `content://`, usa `ClipData`/flags do FileProvider e cancela limpando o temporário;
- URI/arquivo e estado WebView são preservados; rotação permanece liberada;
- somente `CAMERA`, `INTERNET` e estado de rede; sem áudio ou armazenamento amplo;
- QR nativo usa Activity Result API;
- fonte oficial única no módulo `app`; gerador antigo não sobrescreve mais código;
- Gradle Wrapper 8.0 completo, JDK 17, AGP 8.1.0, compile/target 33, minSdk 21, versão Android 2.7.0/27.

## Cloudflare

- Quick Tunnel removido do lançador operacional;
- exemplo `deploy/cloudflared/config.example.yml` com túnel nomeado e fallback 404;
- `setup_cloudflared_named_tunnel.ps1` faz login interativo, cria/reutiliza túnel/DNS, valida ingress e opcionalmente instala o serviço Windows sem embutir token;
- `check_driver_public_endpoint.ps1` valida healthcheck, PWA e lista pública.

## Resultado exato dos gates

| Gate | Resultado |
|---|---|
| Compilação Python + sintaxe JS/Service Worker | PASS |
| Migração `0003 -> 0004` em banco legado sintético, duas execuções | PASS |
| Unit tests (`unittest discover`) | PASS — 54/54 |
| Release gate (11 blocos: auditorias, caos, zero state, UX, Playwright, proxy, stress e unidades) | PASS |
| PWA no navegador local: primeiro acesso, escopo, saída, modal, retrato/paisagem, offline/reenvio | PASS |
| Integridade do teste offline | PASS — 1 operação, 1 problema, pedido `Problema`, carga `Com problema` |
| `gradlew clean lintDebug testDebugUnitTest assembleDebug` | PASS; unit task `NO-SOURCE` |
| Teste Android físico (câmera, cancelamento, Activity recreation, Intents) | NOT RUN — exige aparelho/emulador com câmera/apps |
| APK release assinado | NOT RUN — keystore/senhas da empresa ausentes |
| Cloudflare público/serviço | NOT RUN — domínio e login Cloudflare da empresa ausentes |
| PostgreSQL real | NOT RUN — runtime operacional continua SQLite |

Não há falha automatizada pendente. `lintDebug` permanece com avisos não bloqueantes de dependências antigas/ícone/compatibilidade de backup, registrados no HTML do lint; não foi feito upgrade cego das versões Android.

## APK entregue

- arquivo: `output/apk/Logistica_Casa_do_Campo-v2.7.0-debug.apk`;
- tamanho: 5.985.644 bytes;
- SHA-256: `EC60E28E173C82EC7D447E8EAF8D0ADE33FB6A5D48736010B7836A429F748B92`.

## Passos no Android Studio

1. abrir `android_app_project`;
2. selecionar JDK 17 para Gradle;
3. aguardar Gradle Sync;
4. executar `lintDebug` e `assembleDebug` ou `Build APK(s)`;
5. instalar `app/build/outputs/apk/debug/app-debug.apk`;
6. para produção, configurar keystore fora do Git e gerar release assinado.

## Checklist obrigatório em aparelho

1. configurar servidor manualmente e por QR; reabrir sem nova pergunta;
2. testar rede instável, retry e continuar offline;
3. login `123`, bloqueio e troca obrigatória; expiração/logout;
4. confirmar que dois motoristas não enxergam a carga um do outro;
5. foto pela câmera, cancelamento, galeria, remoção e retorno após rotação/recriação;
6. assinatura em retrato/paisagem e giro durante o desenho;
7. telefone, WhatsApp e mapas em apps externos;
8. entrega normal e problema online;
9. entrega offline, reinício do app, reconexão e ausência de duplicidade;
10. Cloudflare: reiniciar Windows, conferir serviço e rodar `check_driver_public_endpoint.ps1`.
