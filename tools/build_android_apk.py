# -*- coding: utf-8 -*-
"""
tools/build_android_apk.py
==========================
Gerador do Projeto Android Gradle Completo com ZXing Native QR Code Scanner, WebChromeClient PermissionRequest fix,
Logo Casa do Campo, AndroidX e Guia de Deploy.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def generate_android_project(output_dir: Path | None = None) -> Path:
    """Gera o projeto nativo Android Gradle completo para o Android Studio."""
    root = Path(__file__).resolve().parents[1]
    out = output_dir or (root / "android_app_project")
    out.mkdir(parents=True, exist_ok=True)

    # 1. Root settings.gradle
    settings_gradle = """pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "LogisticaCasaDoCampo"
include ':app'
"""

    # 2. Root build.gradle
    root_build_gradle = """plugins {
    id 'com.android.application' version '8.1.0' apply false
    id 'org.jetbrains.kotlin.android' version '1.8.20' apply false
}
"""

    # 3. gradle.properties (Habilita AndroidX e Jetifier)
    gradle_properties = """android.useAndroidX=true
android.enableJetifier=true
org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8
"""

    # 4. app/build.gradle (Inclui zxing-android-embedded para QR Code nativo da câmera)
    app_build_gradle = """plugins {
    id 'com.android.application'
    id 'org.jetbrains.kotlin.android'
}

android {
    namespace 'br.com.casadocampo.logistica'
    compileSdk 33

    defaultConfig {
        applicationId "br.com.casadocampo.logistica"
        minSdk 21
        targetSdk 33
        versionCode 1
        versionName "1.0.0"
    }

    buildTypes {
        release {
            minifyEnabled false
            proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro'
        }
    }
    compileOptions {
        sourceCompatibility JavaVersion.VERSION_1_8
        targetCompatibility JavaVersion.VERSION_1_8
    }
    kotlinOptions {
        jvmTarget = '1.8'
    }
}

dependencies {
    implementation 'androidx.core:core-ktx:1.10.1'
    implementation 'androidx.appcompat:appcompat:1.6.1'
    implementation 'com.google.android.material:material:1.9.0'
    implementation 'com.journeyapps:zxing-android-embedded:4.3.0'
}
"""

    # 5. gradle-wrapper.properties
    gradle_wrapper_properties = """distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-8.0-bin.zip
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
"""

    # 6. AndroidManifest.xml
    manifest_xml = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.CAMERA" />
    <uses-permission android:name="android.permission.RECORD_AUDIO" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
    <uses-permission android:name="android.permission.READ_EXTERNAL_STORAGE" />
    <uses-permission android:name="android.permission.WRITE_EXTERNAL_STORAGE" />

    <uses-feature android:name="android.hardware.camera" android:required="false" />
    <uses-feature android:name="android.hardware.camera.autofocus" android:required="false" />

    <application
        android:allowBackup="true"
        android:icon="@drawable/logo"
        android:label="Logística Casa do Campo"
        android:supportsRtl="true"
        android:hardwareAccelerated="true"
        android:theme="@style/Theme.AppCompat.NoActionBar"
        android:usesCleartextTraffic="true">
        
        <activity
            android:name=".MainActivity"
            android:configChanges="orientation|screenSize|keyboardHidden"
            android:exported="true"
            android:theme="@style/Theme.AppCompat.NoActionBar">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>

        <activity
            android:name="com.journeyapps.zxing.capture.CaptureActivity"
            android:screenOrientation="fullSensor"
            android:stateNotNeeded="true"
            android:theme="@style/zxing_CaptureTheme" />
        <provider
            android:name="androidx.core.content.FileProvider"
            android:authorities="br.com.casadocampo.logistica.fileprovider"
            android:exported="false"
            android:grantUriPermissions="true">
            <meta-data
                android:name="android.support.FILE_PROVIDER_PATHS"
                android:resource="@xml/file_paths" />
        </provider>
    </application>
</manifest>
"""

    # 7. MainActivity.kt (Kotlin Nativo com FileProvider para Câmera Nativa do Android)
    main_activity_kt = """package br.com.casadocampo.logistica

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.webkit.*
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import com.google.zxing.integration.android.IntentIntegrator
import java.io.File
import java.text.SimpleDateFormat
import java.util.*

@Suppress("DEPRECATION")
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private var cameraPhotoPath: String? = null
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

                var takePictureIntent: Intent? = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
                if (takePictureIntent?.resolveActivity(packageManager) != null) {
                    var photoFile: File? = null
                    try {
                        val timeStamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.getDefault()).format(Date())
                        val imageFileName = "JPEG_${timeStamp}_"
                        val storageDir = getExternalFilesDir(Environment.DIRECTORY_PICTURES)
                        photoFile = File.createTempFile(imageFileName, ".jpg", storageDir)
                        cameraPhotoPath = "file:" + photoFile.absolutePath
                    } catch (ex: Exception) {
                        cameraPhotoPath = null
                    }

                    if (photoFile != null) {
                        val photoURI = FileProvider.getUriForFile(
                            this@MainActivity,
                            "br.com.casadocampo.logistica.fileprovider",
                            photoFile
                        )
                        takePictureIntent.putExtra(MediaStore.EXTRA_OUTPUT, photoURI)
                    } else {
                        takePictureIntent = null
                    }
                }

                val contentSelectionIntent = Intent(Intent.ACTION_GET_CONTENT)
                contentSelectionIntent.addCategory(Intent.CATEGORY_OPENABLE)
                contentSelectionIntent.type = "image/*"

                val intentArray: Array<Intent?> = takePictureIntent?.let { arrayOf(it) } ?: arrayOfNulls(0)

                val chooserIntent = Intent(Intent.ACTION_CHOOSER)
                chooserIntent.putExtra(Intent.EXTRA_INTENT, contentSelectionIntent)
                chooserIntent.putExtra(Intent.EXTRA_TITLE, "Tirar Foto ou Selecionar Comprovante")
                chooserIntent.putExtra(Intent.EXTRA_INITIAL_INTENTS, intentArray)

                try {
                    startActivityForResult(chooserIntent, FILE_CHOOSER_REQUEST_CODE)
                } catch (e: Exception) {
                    this@MainActivity.filePathCallback = null
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
            Manifest.permission.RECORD_AUDIO,
            Manifest.permission.READ_EXTERNAL_STORAGE,
            Manifest.permission.WRITE_EXTERNAL_STORAGE
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
                if (data == null || data.data == null) {
                    if (cameraPhotoPath != null) {
                        results = arrayOf(Uri.parse(cameraPhotoPath))
                    }
                } else {
                    val dataString = data.dataString
                    if (dataString != null) {
                        results = arrayOf(Uri.parse(dataString))
                    }
                }
            }
            filePathCallback?.onReceiveValue(results)
            filePathCallback = null
        }
    }
}
"""

    # Grava arquivos do projeto
    with open(out / "settings.gradle", "w", encoding="utf-8") as f:
        f.write(settings_gradle)

    with open(out / "build.gradle", "w", encoding="utf-8") as f:
        f.write(root_build_gradle)

    with open(out / "gradle.properties", "w", encoding="utf-8") as f:
        f.write(gradle_properties)

    wrapper_dir = out / "gradle" / "wrapper"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    with open(wrapper_dir / "gradle-wrapper.properties", "w", encoding="utf-8") as f:
        f.write(gradle_wrapper_properties)

    app_dir = out / "app"
    app_dir.mkdir(parents=True, exist_ok=True)

    with open(app_dir / "build.gradle", "w", encoding="utf-8") as f:
        f.write(app_build_gradle)

    src_dir = app_dir / "src" / "main"
    res_drawable_dir = src_dir / "res" / "drawable"
    res_drawable_dir.mkdir(parents=True, exist_ok=True)

    res_xml_dir = src_dir / "res" / "xml"
    res_xml_dir.mkdir(parents=True, exist_ok=True)
    with open(res_xml_dir / "file_paths.xml", "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n<paths xmlns:android="http://schemas.android.com/apk/res/android">\n    <external-files-path name="my_images" path="Pictures" />\n</paths>\n')

    # Copia logo.png para res/drawable/logo.png
    src_logo = root / "static" / "logo.png"
    if src_logo.exists():
        shutil.copy(src_logo, res_drawable_dir / "logo.png")

    pkg_dir = src_dir / "java" / "br" / "com" / "casadocampo" / "logistica"
    pkg_dir.mkdir(parents=True, exist_ok=True)

    with open(src_dir / "AndroidManifest.xml", "w", encoding="utf-8") as f:
        f.write(manifest_xml)

    with open(pkg_dir / "MainActivity.kt", "w", encoding="utf-8") as f:
        f.write(main_activity_kt)

    print(f"[OK] Projeto Android gerado com sucesso em: {out}")
    return out


if __name__ == "__main__":
    generate_android_project()
