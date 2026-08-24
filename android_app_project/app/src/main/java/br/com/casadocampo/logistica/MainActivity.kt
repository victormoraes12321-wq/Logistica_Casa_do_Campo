package br.com.casadocampo.logistica

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.provider.MediaStore
import android.webkit.*
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.google.zxing.integration.android.IntentIntegrator

@Suppress("DEPRECATION")
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private val FILE_CHOOSER_REQUEST_CODE = 1001
    private val PREFS_NAME = "LogisticaPrefs"
    private val PREF_SERVER_URL = "server_url"

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        
        webView = WebView(this)
        setContentView(webView)

        checkPermissions()

        val settings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.allowFileAccess = true
        settings.allowContentAccess = true
        settings.mediaPlaybackRequiresUserGesture = false
        settings.allowFileAccessFromFileURLs = true
        settings.allowUniversalAccessFromFileURLs = true

        webView.webChromeClient = object : WebChromeClient() {
            // CORRIGE TELA ESCURA AO ABRIR CÂMERA NO WEBVIEW
            override fun onPermissionRequest(request: PermissionRequest?) {
                runOnUiThread {
                    request?.grant(request.resources)
                }
            }

            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>?,
                fileChooserParams: FileChooserParams?
            ): Boolean {
                this@MainActivity.filePathCallback?.onReceiveValue(null)
                this@MainActivity.filePathCallback = filePathCallback

                val takePictureIntent = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
                val chooserIntent = Intent(Intent.ACTION_CHOOSER)
                chooserIntent.putExtra(Intent.EXTRA_INTENT, takePictureIntent)
                chooserIntent.putExtra(Intent.EXTRA_TITLE, "Tirar Foto do Canhoto ou Selecionar Comprovante")

                try {
                    startActivityForResult(chooserIntent, FILE_CHOOSER_REQUEST_CODE)
                } catch (e: Exception) {
                    Toast.makeText(this@MainActivity, "Erro ao abrir câmera: " + e.message, Toast.LENGTH_SHORT).show()
                    return false
                }
                return true
            }
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, url: String?): Boolean {
                if (url != null) view?.loadUrl(url)
                return true
            }

            override fun onReceivedError(view: WebView?, request: WebResourceRequest?, error: WebResourceError?) {
                super.onReceivedError(view, request, error)
                if (request?.isForMainFrame == true) {
                    showServerUrlDialog("Não foi possível conectar ao servidor. Escaneie o QR Code na tela CMD do computador ou informe o link HTTPS:")
                }
            }
        }

        loadSavedServerUrl()
    }

    private fun loadSavedServerUrl() {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        var savedUrl = prefs.getString(PREF_SERVER_URL, null)

        if (savedUrl.isNullOrEmpty() || savedUrl.contains("127.0.0.1")) {
            showServerUrlDialog("Bem-vindo! Escaneie o QR Code na tela do computador (CMD) para conectar o celular instantaneamente:")
        } else {
            if (!savedUrl.endsWith("/static/driver_app/index.html") && !savedUrl.endsWith(".html")) {
                savedUrl = savedUrl.trimEnd('/') + "/static/driver_app/index.html"
            }
            webView.loadUrl(savedUrl)
        }
    }

    private fun showServerUrlDialog(message: String) {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val current = prefs.getString(PREF_SERVER_URL, "") ?: ""

        val input = EditText(this)
        input.hint = "https://xxxx.trycloudflare.com ou https://xxxx.lhr.life"
        input.setText(current)

        AlertDialog.Builder(this)
            .setTitle("⚙️ Conexão do Servidor")
            .setMessage(message)
            .setView(input)
            .setNeutralButton("📷 Escanear QR Code (CMD)") { _, _ ->
                startNativeQrScanner()
            }
            .setPositiveButton("Conectar") { _, _ ->
                var url = input.text.toString().trim()
                if (url.isNotEmpty()) {
                    if (!url.startsWith("http://") && !url.startsWith("https://")) {
                        url = "https://$url"
                    }
                    prefs.edit().putString(PREF_SERVER_URL, url).apply()
                    loadSavedServerUrl()
                } else {
                    Toast.makeText(this, "Por favor, digite um link válido.", Toast.LENGTH_SHORT).show()
                }
            }
            .setNegativeButton("IP Local (Wi-Fi Interno)") { _, _ ->
                prefs.edit().putString(PREF_SERVER_URL, "http://192.168.0.100:3000").apply()
                loadSavedServerUrl()
            }
            .setCancelable(false)
            .show()
    }

    private fun startNativeQrScanner() {
        val integrator = IntentIntegrator(this)
        integrator.setPrompt("Aponte a câmera para o QR Code exibido na tela CMD do computador")
        integrator.setBeepEnabled(true)
        integrator.setOrientationLocked(false)
        integrator.setCameraId(0)
        integrator.initiateScan()
    }

    private fun checkPermissions() {
        val permissions = arrayOf(
            Manifest.permission.CAMERA,
            Manifest.permission.INTERNET,
            Manifest.permission.ACCESS_NETWORK_STATE,
            Manifest.permission.RECORD_AUDIO
        )
        val needed = permissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), 200)
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        val result = IntentIntegrator.parseActivityResult(requestCode, resultCode, data)
        if (result != null) {
            if (result.contents != null) {
                var scannedUrl = result.contents.trim()
                if (!scannedUrl.startsWith("http://") && !scannedUrl.startsWith("https://")) {
                    scannedUrl = "https://$scannedUrl"
                }
                val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                prefs.edit().putString(PREF_SERVER_URL, scannedUrl).apply()
                Toast.makeText(this, "⚙️ Servidor Conectado via QR Code!", Toast.LENGTH_SHORT).show()
                loadSavedServerUrl()
            } else {
                showServerUrlDialog("Escaneamento cancelado. Digite o link HTTPS ou tente novamente:")
            }
            return
        }

        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == FILE_CHOOSER_REQUEST_CODE) {
            if (filePathCallback == null) return
            var results: Array<Uri>? = null
            if (resultCode == RESULT_OK) {
                data?.data?.let {
                    results = arrayOf(it)
                }
            }
            filePathCallback?.onReceiveValue(results)
            filePathCallback = null
        }
    }
}
