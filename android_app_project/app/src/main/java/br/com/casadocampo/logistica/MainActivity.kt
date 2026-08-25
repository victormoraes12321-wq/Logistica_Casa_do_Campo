package br.com.casadocampo.logistica

import android.Manifest
import android.annotation.SuppressLint
import android.content.ClipData
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.Log
import android.webkit.PermissionRequest
import android.webkit.SslErrorHandler
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

@Suppress("DEPRECATION")
class MainActivity : AppCompatActivity() {
    private lateinit var webView: WebView
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private var cameraPhotoUri: Uri? = null
    private var cameraPhotoFile: File? = null
    private var connectionDialogVisible = false
    private var cameraPermissionForFileChooser = false
    private var connectionChecked = false
    private var lastConnectionHealthy = false
    private var lastBackendVersion: String? = null
    private val ioExecutor = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())
    private val qrLauncher = registerForActivityResult(ScanContract()) { result ->
        val contents = result.contents
        if (contents.isNullOrBlank()) {
            showServerDialog("Leitura cancelada. Digite o endereço ou tente novamente.")
        } else {
            val origin = normalizeOrigin(contents)
            if (origin == null) {
                showServerDialog("O QR Code não contém um endereço HTTPS válido.")
            } else {
                healthCheck(origin) { healthy ->
                    if (healthy) {
                        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
                            .putString(PREF_SERVER_ORIGIN, origin).apply()
                        Toast.makeText(this, "Servidor validado.", Toast.LENGTH_SHORT).show()
                        loadApplication(origin)
                    } else showServerDialog("O QR Code foi lido, mas o servidor não respondeu em /healthz.")
                }
            }
        }
    }

    companion object {
        private const val TAG = "LogisticaApp"
        private const val FILE_CHOOSER_REQUEST = 1001
        private const val CAMERA_PERMISSION_REQUEST = 1002
        private const val PREFS_NAME = "LogisticaPrefs"
        private const val PREF_SERVER_ORIGIN = "server_origin"
        private const val LEGACY_PREF_SERVER_URL = "server_url"
        private const val APP_PATH = "/static/driver_app/index.html"
        private const val STATE_PHOTO_URI = "camera_photo_uri"
        private const val STATE_PHOTO_FILE = "camera_photo_file"
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        cameraPhotoUri = savedInstanceState?.getString(STATE_PHOTO_URI)?.let(Uri::parse)
        cameraPhotoFile = savedInstanceState?.getString(STATE_PHOTO_FILE)?.let(::File)
        cleanupStaleCameraFiles()

        webView = WebView(this)
        setContentView(webView)
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
        configureWebView()
        val restored = savedInstanceState?.let { webView.restoreState(it) }
        if (restored == null) restoreOrConfigureServer()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        with(webView.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            allowFileAccess = false
            allowContentAccess = true
            allowFileAccessFromFileURLs = false
            allowUniversalAccessFromFileURLs = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            mediaPlaybackRequiresUserGesture = true
            cacheMode = WebSettings.LOAD_DEFAULT
            setSupportMultipleWindows(false)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) safeBrowsingEnabled = true
            userAgentString = "$userAgentString LogisticaCasaDoCampo/${BuildConfig.VERSION_NAME}"
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest?) {
                // O PWA não usa WebRTC; câmera e galeria passam pelo seletor nativo.
                request?.deny()
            }

            override fun onShowFileChooser(
                view: WebView?,
                callback: ValueCallback<Array<Uri>>?,
                params: FileChooserParams?
            ): Boolean {
                filePathCallback?.onReceiveValue(null)
                discardPendingCameraFile()
                filePathCallback = callback
                return launchImageChooser()
            }
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                return handleNavigation(request?.url)
            }

            @Suppress("OVERRIDE_DEPRECATION")
            override fun shouldOverrideUrlLoading(view: WebView?, url: String?): Boolean {
                return handleNavigation(url?.let(Uri::parse))
            }

            override fun onReceivedError(view: WebView?, request: WebResourceRequest?, error: WebResourceError?) {
                super.onReceivedError(view, request, error)
                if (request?.isForMainFrame == true) showConnectionIssue()
            }

            override fun onReceivedSslError(view: WebView?, handler: SslErrorHandler?, error: SslError?) {
                handler?.cancel()
                Log.w(TAG, "Conexão TLS rejeitada: certificado inválido para ${error?.url?.let(Uri::parse)?.host.orEmpty()}")
                showConnectionIssue()
            }
        }
    }

    private fun launchImageChooser(skipPermissionRequest: Boolean = false): Boolean {
        val cameraGranted = ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
        if (!skipPermissionRequest && !cameraGranted) {
            cameraPermissionForFileChooser = true
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.CAMERA), CAMERA_PERMISSION_REQUEST)
            return true
        }
        val gallery = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "image/*"
        }
        val camera = if (cameraGranted) createCameraIntent() else null
        val chooser = Intent(Intent.ACTION_CHOOSER).apply {
            putExtra(Intent.EXTRA_INTENT, gallery)
            putExtra(Intent.EXTRA_TITLE, "Fotografar ou escolher comprovante")
            if (camera != null) putExtra(Intent.EXTRA_INITIAL_INTENTS, arrayOf(camera))
        }
        return try {
            startActivityForResult(chooser, FILE_CHOOSER_REQUEST)
            true
        } catch (error: Exception) {
            Log.w(TAG, "Nenhum aplicativo disponível para selecionar imagem", error)
            filePathCallback?.onReceiveValue(null)
            filePathCallback = null
            false
        }
    }

    private fun discardPendingCameraFile() {
        cameraPhotoUri?.let {
            revokeUriPermission(it, Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        cameraPhotoFile?.delete()
        cameraPhotoFile = null
        cameraPhotoUri = null
    }

    private fun cleanupStaleCameraFiles() {
        val cutoff = System.currentTimeMillis() - 24L * 60L * 60L * 1000L
        val directories = listOfNotNull(
            getExternalFilesDir(Environment.DIRECTORY_PICTURES),
            File(filesDir, "camera")
        )
        directories.distinct().forEach { directory ->
            directory.listFiles { file -> file.isFile && file.name.startsWith("COMPROVANTE_") }
                ?.filter { it.lastModified() < cutoff }
                ?.forEach { it.delete() }
        }
    }

    private fun createCameraIntent(): Intent? {
        val intent = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
        if (intent.resolveActivity(packageManager) == null) return null
        return try {
            val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
            val directory = getExternalFilesDir(Environment.DIRECTORY_PICTURES)
                ?: File(filesDir, "camera").apply { mkdirs() }
            cameraPhotoFile = File.createTempFile("COMPROVANTE_${stamp}_", ".jpg", directory)
            cameraPhotoUri = FileProvider.getUriForFile(this, "$packageName.fileprovider", cameraPhotoFile!!)
            intent.putExtra(MediaStore.EXTRA_OUTPUT, cameraPhotoUri)
            intent.clipData = ClipData.newRawUri("comprovante", cameraPhotoUri)
            intent.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION)
            intent
        } catch (error: Exception) {
            Log.e(TAG, "Falha ao preparar arquivo temporário da câmera", error)
            cameraPhotoUri = null
            cameraPhotoFile = null
            null
        }
    }

    private fun handleNavigation(uri: Uri?): Boolean {
        if (uri == null) return true
        val scheme = uri.scheme?.lowercase(Locale.ROOT) ?: return true
        if ((scheme == "http" || scheme == "https") && isAllowedInternalUri(uri)) return false
        val allowedExternal = scheme in setOf("http", "https", "tel", "sms", "geo", "market")
        if (!allowedExternal) {
            Toast.makeText(this, "Link não permitido pelo aplicativo.", Toast.LENGTH_SHORT).show()
            return true
        }
        return try {
            startActivity(Intent(Intent.ACTION_VIEW, uri))
            true
        } catch (_: Exception) {
            Toast.makeText(this, "Não há aplicativo disponível para abrir este link.", Toast.LENGTH_SHORT).show()
            true
        }
    }

    private fun isAllowedInternalUri(uri: Uri): Boolean {
        val saved = savedOrigin()?.let(Uri::parse) ?: return false
        val sameOrigin = uri.scheme.equals(saved.scheme, true) &&
            uri.host.equals(saved.host, true) && effectivePort(uri) == effectivePort(saved)
        val path = uri.path.orEmpty()
        return sameOrigin && (path == APP_PATH || path.startsWith("/static/driver_app/"))
    }

    private fun effectivePort(uri: Uri): Int = when {
        uri.port > 0 -> uri.port
        uri.scheme.equals("https", true) -> 443
        else -> 80
    }

    private fun restoreOrConfigureServer() {
        migrateLegacyPreference()
        val origin = savedOrigin()
        if (origin == null) showServerDialog("Configure o endereço fornecido pela empresa uma única vez.")
        else healthCheckWithRetry(origin, 0, loadWhenHealthy = true)
    }

    private fun migrateLegacyPreference() {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        if (prefs.contains(PREF_SERVER_ORIGIN)) return
        val legacy = normalizeOrigin(prefs.getString(LEGACY_PREF_SERVER_URL, null).orEmpty())
        if (legacy != null) prefs.edit().putString(PREF_SERVER_ORIGIN, legacy).remove(LEGACY_PREF_SERVER_URL).apply()
    }

    private fun savedOrigin(): String? = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        .getString(PREF_SERVER_ORIGIN, null)?.let(::normalizeOrigin)

    private fun normalizeOrigin(raw: String): String? {
        var candidate = raw.trim()
        if (candidate.isBlank()) return null
        if (!candidate.startsWith("http://", true) && !candidate.startsWith("https://", true)) candidate = "https://$candidate"
        return try {
            val uri = Uri.parse(candidate)
            val scheme = uri.scheme?.lowercase(Locale.ROOT)
            val host = uri.host?.lowercase(Locale.ROOT)
            if (host.isNullOrBlank() || uri.userInfo != null || scheme !in setOf("http", "https")) return null
            if (scheme == "http" && (!BuildConfig.DEBUG || !isLocalHost(host))) return null
            val authority = if (uri.port > 0) "$host:${uri.port}" else host
            Uri.Builder().scheme(scheme).encodedAuthority(authority).build().toString().trimEnd('/')
        } catch (_: Exception) { null }
    }

    private fun isLocalHost(host: String): Boolean {
        if (host == "localhost" || host == "127.0.0.1" || host.endsWith(".local")) return true
        val isIpv4Literal = Regex("^(?:\\d{1,3}\\.){3}\\d{1,3}$").matches(host)
        val isIpv6Literal = host.contains(":")
        if (!isIpv4Literal && !isIpv6Literal) return false
        return try {
            val address = InetAddress.getByName(host)
            address.isAnyLocalAddress || address.isLoopbackAddress || address.isSiteLocalAddress || address.isLinkLocalAddress
        } catch (_: Exception) { false }
    }

    private fun showServerDialog(message: String) {
        val input = EditText(this).apply {
            hint = "https://logistica.suaempresa.com.br"
            setText(savedOrigin().orEmpty())
            setSingleLine(true)
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle("Servidor da empresa")
            .setMessage(message)
            .setView(input)
            .setNeutralButton("Escanear QR Code") { _, _ -> requestQrScanner() }
            .setNegativeButton("Cancelar", null)
            .setPositiveButton("Validar e salvar", null)
            .setCancelable(savedOrigin() != null)
            .create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val origin = normalizeOrigin(input.text.toString())
                if (origin == null) {
                    input.error = "Use HTTPS. HTTP é aceito apenas na rede local."
                } else {
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
                    healthCheck(origin) { healthy ->
                        dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                        if (healthy) {
                            getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
                                .putString(PREF_SERVER_ORIGIN, origin).remove(LEGACY_PREF_SERVER_URL).apply()
                            dialog.dismiss()
                            loadApplication(origin)
                        } else input.error = "Servidor não respondeu em /healthz. Confira o endereço."
                    }
                }
            }
        }
        dialog.show()
    }

    private fun requestQrScanner() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            cameraPermissionForFileChooser = false
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.CAMERA), CAMERA_PERMISSION_REQUEST)
        } else startQrScanner()
    }

    private fun startQrScanner() {
        val options = ScanOptions()
            .setPrompt("Aponte para o QR Code de configuração da empresa")
            .setBeepEnabled(false)
            .setOrientationLocked(false)
            .setCameraId(0)
        qrLauncher.launch(options)
    }

    private fun healthCheckWithRetry(origin: String, attempt: Int, loadWhenHealthy: Boolean) {
        healthCheck(origin) { healthy ->
            if (healthy) {
                connectionDialogVisible = false
                if (loadWhenHealthy) loadApplication(origin)
            } else if (attempt < 2) {
                val delay = if (attempt == 0) 1000L else 2500L
                mainHandler.postDelayed({ healthCheckWithRetry(origin, attempt + 1, loadWhenHealthy) }, delay)
            } else showConnectionIssue()
        }
    }

    private fun healthCheck(origin: String, callback: (Boolean) -> Unit) {
        ioExecutor.execute {
            var detectedBackendVersion: String? = null
            var connection: HttpURLConnection? = null
            val healthy = try {
                connection = URL("$origin/healthz").openConnection() as HttpURLConnection
                connection.requestMethod = "GET"
                connection.connectTimeout = 5000
                connection.readTimeout = 5000
                connection.instanceFollowRedirects = false
                connection.setRequestProperty("Accept", "application/json")
                connection.responseCode == 200 && connection.inputStream.bufferedReader().use { body ->
                    val payload = JSONObject(body.readText())
                    detectedBackendVersion = payload.optString("system_version").takeIf { it.isNotBlank() }
                    payload.optBoolean("ok", false) &&
                        payload.optString("service") == "logistica-casa-do-campo" &&
                        payload.optInt("driver_api_version", 0) >= 1
                }
            } catch (error: Exception) {
                Log.w(TAG, "Healthcheck indisponível para ${Uri.parse(origin).host}: ${error.javaClass.simpleName}")
                false
            } finally {
                connection?.disconnect()
            }
            mainHandler.post {
                if (!isFinishing && !isDestroyed) {
                    connectionChecked = true
                    lastConnectionHealthy = healthy
                    if (healthy) lastBackendVersion = detectedBackendVersion
                    callback(healthy)
                }
            }
        }
    }

    private fun loadApplication(origin: String) {
        Log.i(TAG, "Carregando origem validada: ${Uri.parse(origin).host}")
        webView.loadUrl("$origin$APP_PATH")
    }

    private fun showConnectionIssue() {
        if (connectionDialogVisible || isFinishing) return
        val origin = savedOrigin() ?: return showServerDialog("Informe o servidor da empresa.")
        connectionDialogVisible = true
        AlertDialog.Builder(this)
            .setTitle("Servidor temporariamente indisponível")
            .setMessage("O endereço salvo foi mantido. Verifique a rede e tente novamente.")
            .setPositiveButton("Tentar novamente") { _, _ -> connectionDialogVisible = false; healthCheckWithRetry(origin, 0, true) }
            .setNeutralButton("Trocar servidor") { _, _ -> connectionDialogVisible = false; showServerDialog("Informe o novo endereço da empresa.") }
            .setNegativeButton("Continuar offline") { _, _ -> connectionDialogVisible = false; loadApplication(origin) }
            .setOnCancelListener { connectionDialogVisible = false }
            .show()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == CAMERA_PERMISSION_REQUEST) {
            val granted = grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED
            if (cameraPermissionForFileChooser) {
                cameraPermissionForFileChooser = false
                if (!granted) Toast.makeText(this, "Câmera não autorizada; escolha uma imagem da galeria.", Toast.LENGTH_LONG).show()
                launchImageChooser(skipPermissionRequest = true)
            } else if (granted) startQrScanner()
            else Toast.makeText(this, "A câmera é necessária para ler o QR Code.", Toast.LENGTH_LONG).show()
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != FILE_CHOOSER_REQUEST) return
        val callback = filePathCallback ?: return
        val gallerySelected = data?.data != null || data?.clipData != null
        val result = if (resultCode == RESULT_OK) {
            if (gallerySelected) WebChromeClient.FileChooserParams.parseResult(resultCode, data)
            else cameraPhotoUri?.let { arrayOf(it) }
        } else null
        val completedCameraFile = if (!gallerySelected && result != null) cameraPhotoFile else null
        if (result == null || gallerySelected) cameraPhotoFile?.delete()
        callback.onReceiveValue(result)
        filePathCallback = null
        cameraPhotoUri?.let {
            revokeUriPermission(it, Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        cameraPhotoUri = null
        cameraPhotoFile = null
        completedCameraFile?.let { file -> mainHandler.postDelayed({ file.delete() }, 15L * 60L * 1000L) }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putString(STATE_PHOTO_URI, cameraPhotoUri?.toString())
        outState.putString(STATE_PHOTO_FILE, cameraPhotoFile?.absolutePath)
        webView.saveState(outState)
        super.onSaveInstanceState(outState)
    }

    override fun onBackPressed() {
        when {
            webView.canGoBack() -> webView.goBack()
            else -> AlertDialog.Builder(this)
                .setTitle("Sair do aplicativo?")
                .setMessage("Abra Configurações para testar a conexão, trocar o servidor ou encerrar a sessão.")
                .setPositiveButton("Sair") { _, _ -> finish() }
                .setNeutralButton("Configurações") { _, _ -> showAboutDialog() }
                .setNegativeButton("Continuar", null)
                .show()
        }
    }

    private fun showAboutDialog() {
        val origin = savedOrigin()
        val serverLabel = origin?.let(::safeServerLabel) ?: "não configurado"
        val connectionLabel = when {
            !connectionChecked -> "não testada"
            lastConnectionHealthy -> "conectado"
            else -> "indisponível"
        }
        val backendLabel = lastBackendVersion ?: "não informado"
        val options = arrayOf("Testar conexão", "Alterar servidor", "Logout", "Fechar")
        AlertDialog.Builder(this)
            .setTitle("Configurações / Sobre")
            .setMessage(
                "Versão do app: ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})\n" +
                    "Versão do backend: $backendLabel\n" +
                    "Status: $connectionLabel\n" +
                    "Servidor: $serverLabel"
            )
            .setItems(options) { dialog, which ->
                when (which) {
                    0 -> {
                        val configured = savedOrigin()
                        if (configured == null) showServerDialog("Configure o servidor antes de testar a conexão.")
                        else healthCheck(configured) { healthy ->
                            Toast.makeText(
                                this,
                                if (healthy) "Conexão validada com sucesso." else "Servidor indisponível ou incompatível.",
                                Toast.LENGTH_LONG
                            ).show()
                            showAboutDialog()
                        }
                    }
                    1 -> showServerDialog("Informe o endereço permanente fornecido pela empresa.")
                    2 -> webView.evaluateJavascript(
                        "if (typeof DriverApp !== 'undefined') { void DriverApp.logout(); } " +
                            "else { localStorage.removeItem('driver_session_v2'); location.reload(); }",
                        null
                    )
                    else -> dialog.dismiss()
                }
            }
            .show()
    }

    private fun safeServerLabel(origin: String): String = try {
        val uri = Uri.parse(origin)
        val port = if (uri.port > 0) ":${uri.port}" else ""
        "${uri.scheme}://${uri.host}$port"
    } catch (_: Exception) {
        "configurado"
    }

    override fun onDestroy() {
        filePathCallback?.onReceiveValue(null)
        filePathCallback = null
        webView.stopLoading()
        webView.destroy()
        ioExecutor.shutdownNow()
        super.onDestroy()
    }
}
