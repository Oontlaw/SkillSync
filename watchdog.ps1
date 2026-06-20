$BotDir = "C:\Users\ACER\Downloads\SkillSync"
$BotScript = "bot.py"
$VenvPy = Join-Path $BotDir ".venv\Scripts\python.exe"
$LogFile = Join-Path $BotDir "watchdog.log"
$CheckInterval = 30

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content -Path $LogFile -Value $line
}

Log "Watchdog started"

while ($true) {
    $running = Get-Process python* | Where-Object { 
        try { $_.CommandLine -match [regex]::Escape($BotScript) } catch { $false }
    }
    if (-not $running) {
        Log "Bot not running — starting..."
        Start-Process -WindowStyle Hidden -FilePath $VenvPy -ArgumentList $BotScript -WorkingDirectory $BotDir
        Start-Sleep -Seconds 10
        if (Get-Process python* | Where-Object { try { $_.CommandLine -match [regex]::Escape($BotScript) } catch { $false } }) {
            Log "Bot started successfully"
        } else {
            Log "Bot failed to start"
        }
    }
    Start-Sleep -Seconds $CheckInterval
}
