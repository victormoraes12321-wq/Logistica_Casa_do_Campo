param([Parameter(Mandatory = $true)][string]$PublicOrigin)
$ErrorActionPreference = 'Stop'
$origin = $PublicOrigin.Trim().TrimEnd('/')
$originUri = $null
if (-not [Uri]::TryCreate($origin, [UriKind]::Absolute, [ref]$originUri) -or $originUri.Scheme -ne 'https' -or -not $originUri.Host) {
    throw 'O endpoint público deve ser uma origem HTTPS válida.'
}
$health = Invoke-RestMethod -Uri "$origin/healthz" -TimeoutSec 15 -Headers @{ Accept = 'application/json' }
if (-not $health.ok -or $health.status -ne 'ok' -or $health.service -ne 'logistica-casa-do-campo' -or $health.api_version -ne 'v1' -or $health.driver_api_version -ne 1) { throw 'Resposta de /healthz inválida.' }
$pwa = Invoke-WebRequest -Uri "$origin/static/driver_app/index.html" -TimeoutSec 15 -UseBasicParsing
if ($pwa.StatusCode -ne 200 -or $pwa.Content -notmatch 'Entregas Casa do Campo') { throw 'PWA do motorista não foi publicada corretamente.' }
$drivers = Invoke-RestMethod -Uri "$origin/api/v1/driver/all_drivers" -TimeoutSec 15 -Headers @{ Accept = 'application/json' }
if (-not $drivers.ok) { throw 'Endpoint público de seleção de motorista indisponível.' }
[pscustomobject]@{ok=$true;origin=$origin;system_version=$health.system_version;api_version=$health.api_version;driver_api_version=$health.driver_api_version;active_drivers=$drivers.count;checked_at=(Get-Date).ToString('s')} | Format-List
