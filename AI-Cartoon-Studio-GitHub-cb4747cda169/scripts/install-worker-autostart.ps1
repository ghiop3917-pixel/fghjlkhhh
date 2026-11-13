# Выполнять вручную после настройки .env. Старое задание не перезаписывается.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$taskName = 'AI Cartoon Studio Render Worker'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'Задание уже существует. Автоматическая перезапись запрещена.'
}
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument '-m studio worker' -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Только финальный рендер готовых пакетов с VPS' | Out-Null
Write-Host 'Автозапуск Worker добавлен для текущего пользователя.'
