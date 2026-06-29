# Pi Worker — 轮询 Issue 表并自动执行任务 (PowerShell)
# 用法: .\pi_worker.ps1 pi_builder building
#       .\pi_worker.ps1 pi_fixer fixing
#       .\pi_worker.ps1 pi_reviewer reflecting

param(
    [string]$Role = "pi_builder",
    [string]$Domain = "building",
    [int]$Interval = 30
)

$host.UI.RawUI.WindowTitle = "Pi Worker: $Role ($Domain)"
Write-Host "============================================================"
Write-Host " Pi Worker: $Role (domain: $Domain, poll: ${Interval}s)"
Write-Host "============================================================"

while ($true) {
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "[$ts] $Role checking tasks..."

    pi --print "You are $Role, domain $Domain. You are a member of the Plastic Promise multi-agent dev team. Claude is your project manager.`n`nSteps:`n1. Call issue_list(owner='$Role', state='open') to find your tasks. Extract Issue ID from returned JSON (format: issue_<hex12>).`n2. If new task found: call issue_transition('<id>', 'in_progress', reason='$Role accepted')`n3. Call memory_recall(domain_hint='$Domain', query='<keywords from Issue context.files>') to load context`n4. Execute the task using write/edit tools. When done, call issue_transition('<id>', 'review', reason='delivery:<files>') AND memory_store(content='<summary>', memory_type='experience', domain='$Domain')`n5. If no tasks found or no tasks assigned to you with state=open, just reply IDLE.`n`nTeam Protocol: No idle chat. All communication must include Issue ID and file paths. If context is insufficient, transition to NEEDS_CONTEXT — do not guess." `
        --session-id "${Role}_worker" `
        --no-session 2>&1 | Select-Object -Last 5

    Start-Sleep -Seconds $Interval
}
