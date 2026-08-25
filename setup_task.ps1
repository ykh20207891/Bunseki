# 日次データ収集を Windows タスクスケジューラに登録する。
#
#   登録内容:
#     タスク名  : CryptoDataCollect
#     実行時刻  : 毎日 09:10 (JST)  = 00:10 UTC
#                 UTC の日付が変わった直後に取ることで snapshot_date が安定する
#     実行内容  : <このフォルダ>\collect.bat
#     取りこぼし: PC が起動していなかった日は次回起動時に自動実行 (StartWhenAvailable)
#
#   実行方法 (PowerShell):
#     powershell -ExecutionPolicy Bypass -File "setup_task.ps1"
#   解除:
#     powershell -ExecutionPolicy Bypass -File "setup_task.ps1" -Unregister

param(
    [switch]$Unregister,
    [string]$TaskName = "CryptoDataCollect",
    [string]$At = "09:10"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat  = Join-Path $root "collect.bat"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "タスク '$TaskName' を解除しました。"
    } else {
        Write-Host "タスク '$TaskName' は登録されていません。"
    }
    return
}

if (-not (Test-Path $bat)) { throw "collect.bat が見つかりません: $bat" }

$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "CoinGecko top500 daily snapshot to SQLite" -Force | Out-Null

Write-Host "タスク '$TaskName' を登録しました（毎日 $At JST 実行）。"
Write-Host ""
Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State, @{n="NextRun";e={ (Get-ScheduledTaskInfo $_).NextRunTime }} |
    Format-List
