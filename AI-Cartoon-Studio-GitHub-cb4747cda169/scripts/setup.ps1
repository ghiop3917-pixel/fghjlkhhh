$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (Test-Path -LiteralPath '.env') {
    Write-Host 'Файл .env уже существует. Настройки сохранены, перезапись не выполнялась.'
    exit 0
}
$configText = Get-Content -LiteralPath '.env.example' -Raw
function New-StudioToken {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
}
$ownerToken = New-StudioToken
$renderToken = New-StudioToken
$configText = $configText.Replace("STUDIO_API_TOKEN=`n", "STUDIO_API_TOKEN=$ownerToken`n").Replace("STUDIO_API_TOKEN=`r`n", "STUDIO_API_TOKEN=$ownerToken`r`n")
$configText = $configText.Replace("STUDIO_WORKER_TOKEN=`n", "STUDIO_WORKER_TOKEN=$renderToken`n").Replace("STUDIO_WORKER_TOKEN=`r`n", "STUDIO_WORKER_TOKEN=$renderToken`r`n")
[System.IO.File]::WriteAllText((Join-Path $projectRoot '.env'), $configText)
Write-Host 'Создан .env с отдельными случайными токенами. Токены не выводятся. Серверные процессы и облака отключены.'
