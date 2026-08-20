param(
  [string]$TaskPrefix = "LogisticaCasaDoCampo"
)

$ErrorActionPreference = "Continue"

$tasks = @(
  "$TaskPrefix-Watchdog",
  "$TaskPrefix-BackupDaily",
  "$TaskPrefix-BackupVerifyWeekly"
)

foreach ($taskName in $tasks) {
  try {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop | Out-Null
    Write-Host "Removida: $taskName"
  }
  catch {
    Write-Host "Não encontrada: $taskName"
  }
}

Write-Host "Concluído."
