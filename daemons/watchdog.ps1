# Watchdog — 每 30s 检查 daemons/pi_daemon.py (通过 PID 文件)
param([int]$Interval = 30)

$PID_FILE = "pi_daemon.pid"
$DAEMON_SCRIPT = "daemons/pi_daemon.py"
$host.UI.RawUI.WindowTitle = "Watchdog"
Write-Host "=== Watchdog: guarding $DAEMON_SCRIPT (PID file: $PID_FILE) ==="

while ($true) {
    if (Test-Path $PID_FILE) {
        $pid = Get-Content $PID_FILE
        $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] pi_daemon OK (PID: $pid)"
        } else {
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] pi_daemon DIED — restarting..."
            Start-Process python -ArgumentList $DAEMON_SCRIPT -WorkingDirectory (Get-Location)
        }
    } else {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] No PID file — starting $DAEMON_SCRIPT..."
        Start-Process python -ArgumentList $DAEMON_SCRIPT -WorkingDirectory (Get-Location)
    }
    Start-Sleep -Seconds $Interval
}
