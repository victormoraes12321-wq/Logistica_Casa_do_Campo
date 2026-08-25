# Android — Logística Casa do Campo

Versão atual: **2.7.1** (`versionCode 28`). O aplicativo é um contêiner Android seguro para o app web do motorista. Na primeira abertura, ele recebe a origem do servidor por QR Code ou digitação manual, valida `GET /healthz`, grava somente a origem em preferências privadas e abre `/static/driver_app/index.html` na WebView.

## Requisitos

- Android Studio **Hedgehog 2023.1.1 ou mais recente**, sem necessidade de atualizar o Gradle do projeto;
- JDK **17** selecionado para o Gradle;
- Android SDK Platform **33** e Build Tools compatíveis instalados;
- acesso à internet na primeira sincronização das dependências.

O projeto usa Android Gradle Plugin 8.1.0, Gradle Wrapper 8.0 e Kotlin 1.8.20. Essa combinação requer JDK 17. Não aceite automaticamente uma migração de AGP/Gradle antes da homologação desta versão.

## Abrir

No Android Studio, use **File > Open** e selecione exatamente:

```text
C:\Users\wccto11ti1\Desktop\Logistica_Casa_do_Campo_ROTAS_EM_ROTA_CORRIGIDO\Logistica_Casa_do_Campo_ROTAS_EM_ROTA_CORRIGIDO\android_app_project
```

O módulo correto é `app`. Aguarde o Gradle Sync e confirme o JDK em **Settings > Build, Execution, Deployment > Build Tools > Gradle**.

## Build debug

No Android Studio: **Build > Build Bundle(s) / APK(s) > Build APK(s)**.

No PowerShell, dentro de `android_app_project`:

```powershell
$env:ANDROID_HOME="$env:LOCALAPPDATA\Android\Sdk"
.\gradlew.bat clean testDebugUnitTest lintDebug assembleDebug
```

APK gerado:

```text
app\build\outputs\apk\debug\app-debug.apk
```

O debug permite HTTP apenas para `localhost`, IP privado ou host `.local`, útil para desenvolvimento. A configuração release bloqueia tráfego HTTP e exige HTTPS válido.

## Instalar

Pelo Android Studio, conecte o aparelho com depuração USB habilitada, selecione-o e pressione **Run**.

Por ADB:

```powershell
adb devices
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

Para uma instalação realmente limpa, desinstale antes o pacote `br.com.casadocampo.logistica`; isso também remove servidor, sessão e fila offline armazenados no aparelho.

## Configuração inicial

1. Abra o app e autorize a câmera apenas se usar o leitor de QR.
2. Leia o QR Code ou digite somente a origem, por exemplo `https://logistica.empresa.com.br`, sem `/static/...`.
3. O app aceita a configuração somente quando `/healthz` responde HTTP 200 com o serviço e a versão de API esperados.
4. Após salvar, o endereço permanece no aparelho. Falha transitória, modo offline ou reinício não apagam a configuração.
5. Para abrir **Configurações / Sobre**, pressione Voltar na tela raiz. A tela mostra versão do app, versão do backend quando disponível, estado da conexão e servidor sem credenciais; também permite testar, alterar servidor e fazer logout.

## Cloudflare Tunnel

Produção deve usar um hostname permanente associado a um Cloudflare Tunnel configurado, por exemplo `https://logistica.empresa.com.br`. Não use endereços temporários `trycloudflare.com`.

Quando o backend estiver no Windows Server, siga também `WINDOWS_SERVER_DEPLOY.md`. O app continua igual: somente o servidor que responde pelo hostname permanente muda.

Antes de distribuir o APK, valide externamente:

```powershell
Invoke-RestMethod https://logistica.empresa.com.br/healthz
```

A resposta deve indicar `ok: true`, `service: logistica-casa-do-campo` e `driver_api_version` igual ou superior a 1. O certificado TLS precisa ser válido; o app cancela erros SSL e nunca os ignora.

## Câmera, upload e FileProvider

O Manifest solicita `CAMERA` para QR Code e captura de comprovante. A galeria usa o seletor de documentos, sem permissão ampla de armazenamento. Fotos da câmera passam por `FileProvider`, com URI temporária e acesso restrito às pastas de comprovantes do próprio app. O usuário pode fotografar, escolher da galeria, cancelar e conferir a prévia antes do envio.

## WebView e navegação

- JavaScript e DOM Storage ficam habilitados porque o app do motorista depende deles;
- conteúdo misto, acesso a arquivos locais, WebRTC e múltiplas janelas ficam bloqueados;
- a WebView aceita navegação interna somente na origem salva e em `/static/driver_app/`;
- HTTP(S) externo, telefone, SMS, mapas e links de marketplace abrem em aplicativo externo;
- esquemas desconhecidos são recusados;
- Voltar percorre o histórico; na raiz abre o menu de saída/configurações;
- cache e fila offline permanecem no perfil privado da WebView.

## Release e assinatura

Não há chave privada no repositório. Gere e guarde a chave fora desta pasta, com backup seguro:

```powershell
keytool -genkeypair -v -keystore D:\segredos\logistica-upload.jks -alias logistica-upload -keyalg RSA -keysize 2048 -validity 10000
```

Crie localmente `android_app_project\keystore.properties` (o arquivo é ignorado pelo Git):

```properties
storeFile=D:/segredos/logistica-upload.jks
storePassword=SENHA_LOCAL
keyAlias=logistica-upload
keyPassword=SENHA_LOCAL
```

Como alternativa, defina `LOGISTICA_RELEASE_STORE_FILE`, `LOGISTICA_RELEASE_STORE_PASSWORD`, `LOGISTICA_RELEASE_KEY_ALIAS` e `LOGISTICA_RELEASE_KEY_PASSWORD` no ambiente. Com as quatro informações disponíveis:

```powershell
.\gradlew.bat clean lintRelease assembleRelease
```

Sem essa configuração, o projeto continua sincronizando e compilando debug, mas não existe release pronta para publicação. Nunca envie `.jks`, `.keystore` ou `keystore.properties` ao Git ou ao ChatGPT.

### Publicação no Google Play

O `targetSdk 33` atual foi preservado para não introduzir uma migração grande antes da homologação e é suficiente para instalação direta/interna. Ele **não atende à política atual de envio comum ao Google Play**. Antes de publicar na loja, planeje uma etapa separada para elevar compile/target SDK, AGP e Gradle de forma compatível e repetir toda a homologação. A política anunciada passa a exigir API 36 para novos apps e atualizações de celular a partir de 31/08/2026: <https://developer.android.com/google/play/requirements/target-sdk>.

## Troubleshooting

- **Servidor:** confirme HTTPS, DNS, certificado e `/healthz`; falha não apaga a origem salva. Use Configurações > Testar conexão.
- **Câmera/QR:** confira a permissão em Configurações do Android. Se a câmera de comprovante for negada, a galeria continua disponível.
- **Assinatura:** confira os quatro campos e o caminho externo da chave. Senha errada aparece como falha de `validateSigningRelease`.
- **Login:** motorista entra pelo nome e senha temporária `123`; o primeiro acesso obriga troca de senha antes de liberar as cargas.
- **Offline:** continue offline somente após o app já ter carregado ao menos uma vez. Registros pendentes permanecem no aparelho.
- **Sincronização:** volte à internet e use **Sincronizar**; não limpe dados nem desinstale enquanto houver pendências.
- **WebView:** mantenha Android System WebView/Chrome atualizado no aparelho.

## Logs

No Android Studio, abra **Logcat** e filtre por `package:br.com.casadocampo.logistica` ou pela tag `LogisticaApp`.

Por terminal:

```powershell
adb logcat -c
adb logcat --pid=$(adb shell pidof -s br.com.casadocampo.logistica)
```

Antes de compartilhar, remova tokens, cabeçalhos `Authorization`, senhas, nomes/telefones de clientes, endereços, fotos, assinaturas e conteúdo de entregas. O código nativo registra somente eventos técnicos e hostname, mas logs do sistema ou do servidor podem conter dados adicionais.

## Homologação

Execute o roteiro de [ANDROID_HOMOLOGACAO.md](ANDROID_HOMOLOGACAO.md) em aparelho físico antes de gerar ou distribuir release.
