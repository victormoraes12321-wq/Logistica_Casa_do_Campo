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

        webView.webChromeClient = object : WebChromeClient() {
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
                chooserIntent.putExtra(Intent.EXTRA_TITLE, "Selecione a Câmera ou Comprovante")

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
                    showServerUrlDialog("Não foi possível conectar ao servidor. Digite o link HTTPS gerado pelo computador da empresa:")
                }
            }
        }

        loadSavedServerUrl()
    }

    private fun loadSavedServerUrl() {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        var savedUrl = prefs.getString(PREF_SERVER_URL, null)

        if (savedUrl.isNullOrEmpty() || savedUrl.contains("127.0.0.1")) {
            showServerUrlDialog("Digite o link HTTPS do servidor da empresa (gerado pelo script no computador):")
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
        input.hint = "https://xxxx-yyyy.trycloudflare.com"
        input.setText(current)

        AlertDialog.Builder(this)
            .setTitle("⚙️ Servidor da Empresa")
            .setMessage(message)
            .setView(input)
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
            .setNegativeButton("Servidor Local (Rede interna)") { _, _ ->
                prefs.edit().putString(PREF_SERVER_URL, "http://192.168.0.100:3000").apply()
                loadSavedServerUrl()
            }
            .setCancelable(false)
            .show()
    }

    private fun checkPermissions() {
        val permissions = arrayOf(
            Manifest.permission.CAMERA,
            Manifest.permission.INTERNET,
            Manifest.permission.ACCESS_NETWORK_STATE
        )
        val needed = permissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), 200)
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
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
