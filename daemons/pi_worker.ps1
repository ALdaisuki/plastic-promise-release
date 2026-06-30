# Pi Worker — SuperPowers Pipeline mode-based dispatch
param(
    [string]$Mode = "builder",
    [int]$Interval = 30
)

$modeMap = @{
    "planner"  = @{ Role="pi_planner";  Domain="designing";  Query="tag:spec domain:designing";       OutputTags="task:plan", "assignee:pi_builder", "domain:building" }
    "builder"  = @{ Role="pi_builder";  Domain="building";   Query="tag:plan domain:building";        OutputTags="task:active", "owner:pi_builder", "domain:building" }
    "fixer"    = @{ Role="pi_fixer";    Domain="fixing";     Query="tag:rejected domain:fixing";      OutputTags="task:fixed", "owner:pi_fixer", "domain:fixing" }
    "reviewer" = @{ Role="pi_reviewer"; Domain="reflecting"; Query="tag:active domain:building";      OutputTags="task:review", "owner:pi_reviewer", "domain:reflecting" }
}

$cfg = $modeMap[$Mode]
if (-not $cfg) {
    Write-Error "Unknown mode: $Mode. Valid: planner, builder, fixer, reviewer"
    exit 1
}

$host.UI.RawUI.WindowTitle = "Pi Worker: $Mode ($($cfg.Domain))"
Write-Host "============================================================"
Write-Host " Pi Worker: $Mode ($($cfg.Role), domain=$($cfg.Domain))"
Write-Host " Query: $($cfg.Query)"
Write-Host " Output: $($cfg.OutputTags -join ', ')"
Write-Host "============================================================"

while ($true) {
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "[$ts] $Mode searching..."

    if ($Mode -eq "reviewer") {
        $prompt = @"
You are $($cfg.Role), domain $($cfg.Domain), mode=$Mode.

SuperPowers pipeline stage: requesting-code-review. You are the code reviewer in the Plastic Promise multi-agent team. Claude is the PM.

## Review Protocol (see .pi/team-protocol-reviewer.md)

1. Call memory_recall(domain_hint='reflecting', query='task:review domain:reflecting assignee:pi_reviewer') to find pending review tasks.
2. For each review task, extract commit_range from tags (format: commit:HEAD~N..HEAD).
3. Call review_run(action='prepare', commit_range='<extracted>') to get the diff + pre-checks + review prompt.
4. Execute the review:
   - Read the full diff
   - Check each changed file against the 12 principles (see review prompt)
   - Run the security checklist (injection, hardcoded keys, input validation, permissions, error leakage, dependencies)
   - Identify specific findings with severity, file, line_range, description, and suggestion
5. Output ONLY the strict JSON review report (no markdown, no commentary outside JSON).
6. Call review_run(action='full', commit_range='<extracted>', review_output='<your JSON report>', author_target='pi_builder', reviewer_target='pi_reviewer')
   -> This automatically: parses your report, adjusts trust scores, stores findings as memories, creates fix tasks for blockers/majors.
7. If no review tasks found, reply IDLE.

CRITICAL: Do NOT output markdown. Your final output must be valid JSON matching the review report schema.
"@
        pi --print $prompt --session-id "${Mode}_worker" 2>&1 | Select-Object -Last 5
    } else {
        pi --print "You are $($cfg.Role), domain $($cfg.Domain), mode=$Mode.`n`nSuperPowers pipeline stage: $Mode.`n`n1. Call memory_recall(domain_hint='$($cfg.Domain)', query='$($cfg.Query)') to find your input.`n2. Process it according to your pipeline stage.`n3. When done, call memory_store(content='<summary>', memory_type='experience', domain='$($cfg.Domain)', tags=[$($cfg.OutputTags | ForEach-Object { "'$_'" } | Join-String -Separator ',')]).`n`nIf no matching memories found, reply IDLE." `
        --session-id "${Mode}_worker" 2>&1 | Select-Object -Last 5

    Start-Sleep -Seconds $Interval
}
