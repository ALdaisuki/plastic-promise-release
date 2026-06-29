# Watchdog — 每 60s 检查 pi_daemon.py，挂了自动重启
param([int]$Interval = 60)

$host.UI.RawUI.WindowTitle = "Watchdog (pi_daemon guard)"
Write-Host "=== Watchdog: guarding pi_daemon.py every ${Interval}s ==="

while ($true) {
    $running = Get-Process python -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -match "pi_daemon" -or $_.MainWindowTitle -match "daemon"
    }

    if (-not $running) {
        $ts = Get-Date -Format "HH:mm:ss"
        Write-Host "[$ts] pi_daemon NOT RUNNING — restarting..."
        Start-Process python -ArgumentList "pi_daemon.py" -WindowStyle Minimized
        Write-Host "[$ts] Restarted pi_daemon.py"
    } else {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] pi_daemon OK (PID: $($running.Id))"
    }
    Start-Sleep -Seconds $Interval
}
