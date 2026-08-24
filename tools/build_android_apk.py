# -*- coding: utf-8 -*-
"""
tools/build_android_apk.py
==========================
Gerador do Projeto Android Gradle Completo com Logo Casa do Campo, AndroidX e Guia de Deploy.
Gera a estrutura nativa Android Studio reconhecida com suporte a Gradle, Câmera e WebView.
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

    # 4. app/build.gradle
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
}
"""

    # 5. gradle-wrapper.properties
    gradle_wrapper_properties = """distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-8.0-bin.zip
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
"""

    # 6. AndroidManifest.xml (Usando o ícone do sistema ou a logo da empresa)
    manifest_xml = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.CAMERA" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
    <uses-permission android:name="android.permission.READ_EXTERNAL_STORAGE" />
    <uses-permission android:name="android.permission.WRITE_EXTERNAL_STORAGE" />

    <application
        android:allowBackup="true"
        android:icon="@drawable/logo"
        android:label="Logística Casa do Campo"
        android:supportsRtl="true"
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
    </application>
</manifest>
"""

    # 7. MainActivity.kt (Kotlin com Câmera e WebChromeClient FileChooser)
    main_activity_kt = """package br.com.casadocampo.logistica

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.provider.MediaStore
import android.webkit.*
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private val FILE_CHOOSER_REQUEST_CODE = 1001

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
        }

        // Carrega o App do Motorista
        val targetUrl = intent.getStringExtra("SERVER_URL") ?: "http://127.0.0.1:3000/static/driver_app/index.html"
        webView.loadUrl(targetUrl)
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
