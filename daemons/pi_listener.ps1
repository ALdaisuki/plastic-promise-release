# Pi Listener — SSE 事件驱动 Worker (替代轮询)
# 用法: .\pi_listener.ps1 pi_builder building

param(
    [string]$Role = "pi_builder",
    [string]$Domain = "building",
    [int]$Cooldown = 10
)

$host.UI.RawUI.WindowTitle = "Pi Listener: $Role ($Domain) [event-driven]"
Write-Host "============================================================"
Write-Host " Pi Listener: $Role (domain: $Domain, SSE-driven)"
Write-Host "============================================================"

while ($true) {
    Write-Host "Listening for tasks via SSE /events..."

    # 连接 SSE 事件流，等待任务通知
    $task = curl -s -N "http://127.0.0.1:9020/events" 2>$null | ForEach-Object {
        $line = $_.ToString()
        if ($line -match "data:") {
            $data = $line -replace "^data: ", ""
            try {
                $json = $data | ConvertFrom-Json
                if ($json.type -eq "issue_transition" -or $json.type -eq "memory_stored") {
                    $json
                    break
                }
            } catch {}
        }
    }

    if ($task) {
        $ts = Get-Date -Format "HH:mm:ss"
        Write-Host "[$ts] New task detected! $Role executing..."

        pi --print "You are $Role, domain $Domain. A new task was detected via SSE.`n`nSteps:`n1. Call memory_recall(domain_hint='$Domain', query='TASK for $Role pending') to find your assigned task.`n2. Execute the task using write/edit/bash tools.`n3. Call memory_store(content='$Role DONE: <summary>', memory_type='experience', domain='$Domain', tags=['done','$Role']) to report completion." `
            --session-id "${Role}_listener" 2>&1 | Select-Object -Last 8
    }

    Start-Sleep -Seconds $Cooldown
}
