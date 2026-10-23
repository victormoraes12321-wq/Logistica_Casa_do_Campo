$env:JAVA_HOME = "C:\Program Files\Microsoft\jdk-17.0.20.101-hotspot"
$env:ANDROID_HOME = "C:\Users\Administrator\AppData\Local\Android\Sdk"
$env:PATH = "$env:JAVA_HOME\bin;" + $env:PATH
Write-Output "JAVA_HOME=$env:JAVA_HOME"
Write-Output "ANDROID_HOME=$env:ANDROID_HOME"
& .\gradlew.bat assembleDebug
if ($LASTEXITCODE -eq 0) {
    Copy-Item -Path "app\build\outputs\apk\debug\app-debug.apk" -Destination "..\static\Logistica_Casa_do_Campo.apk" -Force
    Copy-Item -Path "app\build\outputs\apk\debug\app-debug.apk" -Destination "..\static\app-debug.apk" -Force
    Write-Output "BUILD SUCCESS AND APK COPIED TO STATIC!"
} else {
    Write-Output "BUILD FAILED WITH CODE $LASTEXITCODE"
}
