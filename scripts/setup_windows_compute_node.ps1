[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$SourceRevision,

    [string]$ProfilePath = '',

    [ValidateSet('all', 'preflight', 'ollama', 'models', 'build', 'env', 'verify')]
    [string]$Stage = 'all',

    [switch]$SkipPreflight,

    [switch]$SkipOllamaStart,

    [switch]$SkipGpuSmoke,

    [switch]$RecreateDedicatedBuilder
)

<#
.SYNOPSIS
    Idempotent Windows/WSL2 compute-node bootstrap for the split-accelerated
    profile: registers persistent scheduled tasks (Ollama serve, pinned model
    sync, immutable image build) and writes the compose .env identity.

.DESCRIPTION
    The operator machine must already have Docker Desktop or a WSL2-native
    Docker daemon, a Windows Python with huggingface_hub, and (for the ollama
    embedding backend) the local Ollama executable. This script never writes
    credentials, model weights, or private endpoints into the repository;
    operator values live in the node-local profile file referenced by
    -ProfilePath.

    Stages:
      preflight run the host preflight (docker runtime, disk/VHDX, .wslconfig,
               systemd, proxy); the optional Docker bridge is not required
      all      register + start the three persisted tasks, then write .env
      ollama   (re)register PPOllamaServe, start it, wait for /api/tags
      models   run the pinned rerank model sync once (used by PPNodeModelSync)
      build    run the pinned image build once (used by PPNodeBuild)
      env      (re)generate deploy/local-inference-node/.env from the profile
      verify   start the compose node and run the full smoke suite

    The build resource gate intentionally defers while model sync or another
    build is active; re-run `-Stage build` (or the PPNodeBuild task) after the
    model sync log shows PP_NODE_MODEL_SYNC_COMPLETE.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-PpLog {
    param([string]$Message)
    Write-Output ("[setup-windows-compute-node] " + $Message)
}

function Read-PpProfile {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-PpLog "profile not found: $Path (using built-in defaults)"
        return $map
    }
    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#')) {
            $idx = $line.IndexOf('=')
            if ($idx -gt 0) {
                $key = $line.Substring(0, $idx).Trim()
                $value = $line.Substring($idx + 1).Trim()
                if ($key) { $map[$key] = $value }
            }
        }
    }
    return $map
}

function Get-PpValue {
    param($Map, [string]$Key, [string]$Default = '')
    if ($Map.ContainsKey($Key) -and -not [string]::IsNullOrWhiteSpace([string]$Map[$Key])) {
        return [string]$Map[$Key]
    }
    return $Default
}

function ConvertFrom-PpContainerModelReference {
    param([string]$Reference)
    if ($Reference -notmatch '^/models/([^\r\n]+)$') {
        throw 'setup_windows_compute_node_model_reference_invalid'
    }
    $relative = $Matches[1]
    if ($relative -match '(^|/|\\)\.\.($|/|\\)' -or $relative.StartsWith('/')) {
        throw 'setup_windows_compute_node_model_reference_invalid'
    }
    return $relative
}

function Protect-PpPrivateFile {
    param([string]$Path)
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $Path /inheritance:r /grant:r `
        "*$($currentSid):(F)" `
        "*S-1-5-18:(F)" `
        "*S-1-5-32-544:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'setup_windows_compute_node_private_acl_failed'
    }
}

function Get-PpPython {
    $candidates = @(
        (Get-PpValue $profile 'PP_PYTHON_EXECUTABLE' ''),
        ''
    )
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        $candidates += [string]$cmd.Source
    }
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher -and $pyLauncher.Source) {
        try {
            $resolved = & $pyLauncher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path -LiteralPath $resolved)) {
                $candidates += [string]$resolved
            }
        }
        catch { }
    }
    $candidates += "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
    $candidates += "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    $candidates += "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return [string]$candidate }
    }
    throw 'setup_windows_compute_node_python_not_available'
}

function ConvertTo-PpWslPath {
    param([string]$WindowsPath)
    $drive = $WindowsPath.Substring(0, 1).ToLowerInvariant()
    return '/mnt/' + $drive + '/' + $WindowsPath.Substring(3).Replace('\', '/')
}

function Invoke-PpDocker {
    param([object[]]$Arguments)
    $prefix = if ($script:dockerCommand -eq 'docker.exe') {
        @('docker.exe')
    }
    elseif ($script:dockerCommand -match '^wsl\.exe -d ([A-Za-z0-9._-]+) -e docker$') {
        @('wsl.exe', '-d', $matches[1], '-e', 'docker')
    }
    else {
        throw 'setup_windows_compute_node_docker_command_invalid'
    }
    $command = $prefix[0]
    $effective = @($prefix | Select-Object -Skip 1) + @($Arguments)
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $output = & $command @effective 2>&1
        $text = ($output | ForEach-Object { $_.ToString() }) -join "`n"
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $text.Trim() }
    }
    catch {
        return [pscustomobject]@{ ExitCode = -1; Output = $_.Exception.Message }
    }
    finally {
        $ErrorActionPreference = $oldEap
    }
}

function Resolve-PpDockerCommand {
    param($Map, [string]$ReportPath)
    $configured = Get-PpValue $Map 'PP_DOCKER_COMMAND' ''
    if ($configured) { return $configured }
    if ($ReportPath -and (Test-Path -LiteralPath $ReportPath)) {
        try {
            $reported = [string](Get-Content -LiteralPath $ReportPath -Raw | ConvertFrom-Json).docker_command
            if ($reported) { return $reported }
        }
        catch { }
    }
    return 'docker.exe'
}

function Resolve-PpProxyUrl {
    param($Map, [string]$ReportPath)
    if ($ReportPath -and (Test-Path -LiteralPath $ReportPath)) {
        try {
            $reported = [string](Get-Content -LiteralPath $ReportPath -Raw | ConvertFrom-Json).proxy_url
            if ($reported) { return $reported }
        }
        catch { }
    }
    return Get-PpValue $Map 'PP_PROXY_URL' ''
}

function Assert-PpDockerCommand {
    param([string]$Value)
    if ($Value -eq 'docker.exe') { return }
    if ($Value -match '^wsl\.exe -d [A-Za-z0-9._-]+ -e docker$') { return }
    throw 'setup_windows_compute_node_docker_command_invalid'
}

function Test-PpRerankTreeComplete {
    param([string]$Target)
    $indexPath = Join-Path $Target 'model.safetensors.index.json'
    if ($script:dockerCommand -notmatch 'wsl\.exe') {
        return Test-Path -LiteralPath $indexPath
    }
    $wslIndexPath = ConvertTo-PpWslPath -WindowsPath $indexPath
    & wsl.exe -d $wslDistro -e test -f $wslIndexPath 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Get-PpOllama {
    $candidates = @(
        (Get-PpValue $profile 'PP_OLLAMA_EXECUTABLE' ''),
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
        'C:\Program Files\Ollama\ollama.exe',
        (Get-Command ollama.exe -ErrorAction SilentlyContinue).Source
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    throw 'setup_windows_compute_node_ollama_not_available'
}

function Get-PpDefaultUserProfile {
    $interactive = Get-PpInteractiveUser
    if ($interactive -and $interactive.Contains('\')) {
        $candidate = 'C:\Users\' + $interactive.Split('\')[-1]
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    if ($env:USERPROFILE -and $env:USERPROFILE.StartsWith('C:\Users\', [System.StringComparison]::OrdinalIgnoreCase)) {
        return $env:USERPROFILE
    }
    return ''
}

function Test-PpOllamaTags {
    param([string]$HostUri)
    try {
        $null = Invoke-RestMethod -Uri "$HostUri/api/tags" -TimeoutSec 4
        return $true
    }
    catch {
        return $false
    }
}

function Get-PpInteractiveUser {
    try {
        $cs = Get-CimInstance -ClassName Win32_ComputerSystem
        if ($cs.UserName) { return [string]$cs.UserName }
    }
    catch { }
    return ''
}

function Register-PpTask {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Execute,
        [Parameter(Mandatory = $true)][string]$Argument,
        [string]$UserId = 'SYSTEM',
        [ValidateSet('ServiceAccount', 'Interactive', 'Password')]
        [string]$LogonType = 'ServiceAccount',
        [switch]$UnlimitedTime,
        [int]$RestartCount = 0
    )
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date
    $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType $LogonType -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
    if ($UnlimitedTime) {
        $settings.ExecutionTimeLimit = 'PT0S'
    }
    if ($RestartCount -gt 0) {
        $settings.RestartCount = $RestartCount
        $settings.RestartInterval = 'PT1M'
    }
    $action = New-ScheduledTaskAction -Execute $Execute -Argument $Argument
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Write-PpLog "registered task $Name"
}

function Start-PpTaskOnce {
    param([string]$Name)
    try {
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
        if ($task.State -eq 'Running') {
            Write-PpLog "task $Name already running; not restarted"
            return
        }
        Start-ScheduledTask -TaskName $Name
        Write-PpLog "started task $Name"
    }
    catch {
        Write-PpLog "task $Name start skipped: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Load profile + fixed layout
# ---------------------------------------------------------------------------
$profile = Read-PpProfile -Path $ProfilePath
$wslDistro = Get-PpValue $profile 'PP_WSL_DISTRO' 'Ubuntu-22.04'
if ($wslDistro -notmatch '^[A-Za-z0-9._-]+$') {
    throw 'setup_windows_compute_node_wsl_distro_invalid'
}
$workspace = Get-PpValue $profile 'PP_PLASTIC_PROMISE_WORKSPACE' (Join-Path $env:USERPROFILE 'PlasticPromise')
$LogRoot = Join-Path $workspace 'logs'
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$defaultUserProfile = Get-PpDefaultUserProfile
$repoRoot = Get-PpValue $profile 'PP_WINDOWS_BUILD_REPOSITORY_ROOT' ''
if (-not $repoRoot) {
    $repoRoot = Join-Path $workspace "remote-builds\$SourceRevision\source"
}
$repoRoot = [System.IO.Path]::GetFullPath($repoRoot)
$setupScript = Join-Path $repoRoot 'scripts\setup_windows_compute_node.ps1'
$buildScript = Join-Path $repoRoot 'scripts\run_windows_local_inference_build.ps1'
$syncScript = Join-Path $repoRoot 'scripts\sync_compute_node_models.py'
$configureScript = Join-Path $repoRoot 'scripts\configure_windows_compute_env.ps1'

$nodeModelDirectory = Get-PpValue $profile 'PP_LOCAL_NODE_MODEL_DIRECTORY' (Join-Path $workspace 'models')
$rerankTarget = Join-Path $nodeModelDirectory 'rerank'
$embeddingBackend = Get-PpValue $profile 'PP_LOCAL_NODE_EMBEDDING_BACKEND' ''
$embeddingModel = Get-PpValue $profile 'PP_LOCAL_NODE_EMBEDDING_MODEL' ''
$embeddingRevision = Get-PpValue $profile 'PP_LOCAL_NODE_EMBEDDING_REVISION' ''
$embeddingDimension = Get-PpValue $profile 'PP_LOCAL_NODE_EMBEDDING_DIMENSION' ''
$embeddingNormalization = Get-PpValue $profile 'PP_LOCAL_NODE_EMBEDDING_NORMALIZATION' 'l2'
$embeddingArtifactSha256 = Get-PpValue $profile 'PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256' ''
$embeddingModelReference = Get-PpValue $profile 'PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE' '/models/embedding'
$rerankBackend = Get-PpValue $profile 'PP_LOCAL_NODE_RERANK_BACKEND' 'llama.cpp'
$rerankModel = Get-PpValue $profile 'PP_LOCAL_NODE_RERANK_MODEL' ''
$rerankRevision = Get-PpValue $profile 'PP_LOCAL_NODE_RERANK_REVISION' ''
$rerankArtifactSha256 = Get-PpValue $profile 'PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256' ''
$rerankModelReference = Get-PpValue $profile 'PP_LOCAL_NODE_RERANK_MODEL_REFERENCE' '/models/rerank'
$structuredJsonBackend = Get-PpValue $profile 'PP_LOCAL_NODE_STRUCTURED_JSON_BACKEND' 'off'
$nodeAuthorization = Get-PpValue $profile 'PP_LOCAL_NODE_AUTHORIZATION' ''
$nodeId = Get-PpValue $profile 'PP_LOCAL_NODE_ID' 'inference-node'
$nodeOllamaHost = Get-PpValue $profile 'PP_LOCAL_NODE_OLLAMA_HOST' ''
$hfEndpoint = Get-PpValue $profile 'HF_ENDPOINT' ''
if (-not $embeddingBackend -or -not $embeddingModel -or -not $embeddingDimension) {
    throw 'setup_windows_compute_node_embedding_identity_required'
}
if (-not $rerankModel -or -not $rerankRevision) {
    throw 'setup_windows_compute_node_rerank_identity_required'
}
if ($nodeId -notmatch '^[a-z][a-z0-9_.:-]{1,127}$') {
    throw 'setup_windows_compute_node_id_invalid'
}
if (-not $nodeOllamaHost) {
    if ($embeddingBackend -eq 'ollama') { $nodeOllamaHost = 'http://host.docker.internal:11434' }
    else { $nodeOllamaHost = 'http://127.0.0.1:11434' }
}

if (-not (Test-Path -LiteralPath $repoRoot)) {
    throw "setup_windows_compute_node_repo_missing: $repoRoot"
}

# Resolve the docker invocation: an explicit profile value wins, then the
# preflight report, then docker.exe. WSL2-native hosts use
# "wsl.exe -d <distro> -e docker", which works without localhost bridging.
$preflightReport = Join-Path $LogRoot 'preflight-report.json'
$script:dockerCommand = Resolve-PpDockerCommand -Map $profile -ReportPath $preflightReport
Assert-PpDockerCommand -Value $script:dockerCommand
$script:proxyUrl = Resolve-PpProxyUrl -Map $profile -ReportPath $preflightReport
Write-PpLog "docker command: $script:dockerCommand"

# ---------------------------------------------------------------------------
# preflight stage (docker runtime / disk / wslconfig / systemd / proxy)
# ---------------------------------------------------------------------------
if ($Stage -in @('all', 'preflight') -and -not $SkipPreflight) {
    $preflightScript = Join-Path $repoRoot 'scripts\preflight_windows_node_host.ps1'
    if (Test-Path -LiteralPath $preflightScript) {
        Write-PpLog 'running host preflight (docker runtime / disk / wslconfig / proxy)'
        $preflightArgs = @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $preflightScript,
            '-WslDistro', $wslDistro,
            '-OutputPath', (Join-Path $LogRoot 'preflight-report.json')
        )
        if ($ProfilePath) {
            $preflightArgs += @('-ProfilePath', $ProfilePath)
        }
        $profileProxy = Get-PpValue $profile 'PP_PROXY_URL' ''
        if ($profileProxy) {
            $preflightArgs += @('-ProxyUrl', $profileProxy)
        }
        $profileVhdxTarget = Get-PpValue $profile 'PP_WSL_VHDX_TARGET' ''
        if ($profileVhdxTarget) {
            $preflightArgs += @('-MigrateVhdxTo', $profileVhdxTarget)
        }
        & powershell.exe @preflightArgs
        if ($LASTEXITCODE -ne 0) {
            throw 'setup_windows_compute_node_preflight_failed'
        }
        $script:dockerCommand = Resolve-PpDockerCommand -Map $profile -ReportPath $preflightReport
        Assert-PpDockerCommand -Value $script:dockerCommand
        $script:proxyUrl = Resolve-PpProxyUrl -Map $profile -ReportPath $preflightReport
        Write-PpLog "docker command after preflight: $script:dockerCommand"
    }
    else {
        Write-PpLog "preflight script not found at $preflightScript; skipped"
    }
}

# ---------------------------------------------------------------------------
# ollama stage
# ---------------------------------------------------------------------------
if ($Stage -in @('all', 'ollama') -and $embeddingBackend -eq 'ollama') {
    $ollamaExe = Get-PpOllama
    $ollamaModels = Get-PpValue $profile 'PP_OLLAMA_MODELS_DIR' (Join-Path $defaultUserProfile '.ollama\models')
    $ollamaHost = Get-PpValue $profile 'PP_OLLAMA_HOST' '0.0.0.0:11434'
    $serveCommand = "cmd /c set OLLAMA_MODELS=$ollamaModels&& set OLLAMA_HOST=$ollamaHost&& `"$ollamaExe`" serve"
    Register-PpTask -Name 'PPOllamaServe' -Execute 'cmd.exe' -Argument $serveCommand `
        -UserId 'SYSTEM' -LogonType ServiceAccount -UnlimitedTime -RestartCount 3
    if (-not $SkipOllamaStart) {
        Start-PpTaskOnce -Name 'PPOllamaServe'
    }
    $tagsReady = Test-PpOllamaTags -HostUri 'http://127.0.0.1:11434'
    if (-not $tagsReady) {
        for ($attempt = 0; $attempt -lt 15 -and -not $tagsReady; $attempt++) {
            Start-Sleep -Seconds 2
            $tagsReady = Test-PpOllamaTags -HostUri 'http://127.0.0.1:11434'
        }
    }
    if (-not $tagsReady -and $embeddingBackend -eq 'ollama') {
        throw 'setup_windows_compute_node_ollama_not_ready'
    }
    if ($embeddingBackend -eq 'ollama') {
        $listed = & $ollamaExe list 2>$null | Out-String
        if ($listed -notmatch [regex]::Escape($embeddingModel)) {
            Write-PpLog "pulling $embeddingModel (long first pull)"
            & $ollamaExe pull $embeddingModel
            if ($LASTEXITCODE -ne 0) { throw 'setup_windows_compute_node_ollama_pull_failed' }
        }
        else {
            Write-PpLog "embedding model already present: $embeddingModel"
        }
    }
    Write-PpLog 'ollama stage complete'
}

# ---------------------------------------------------------------------------
# models stage
# ---------------------------------------------------------------------------
if ($Stage -in @('all', 'models')) {
    $python = Get-PpPython
    $log = Join-Path $LogRoot 'model-sync.log'
    New-Item -ItemType Directory -Force -Path $rerankTarget | Out-Null
    if ($Stage -eq 'all') {
        $argument = "-NoProfile -ExecutionPolicy Bypass -File `"$setupScript`" -SourceRevision $SourceRevision -ProfilePath `"$ProfilePath`" -Stage models"
        Register-PpTask -Name 'PPNodeModelSync' -Execute 'powershell.exe' -Argument $argument `
            -UserId 'SYSTEM' -LogonType ServiceAccount
        Start-PpTaskOnce -Name 'PPNodeModelSync'
        Write-PpLog "model sync task started; completion marker is written to $log"
    }
    else {
        Start-Transcript -Path $log -Append | Out-Null
        try {
            & $python $syncScript `
                --repo-id $rerankModel `
                --revision $rerankRevision `
                --target $rerankTarget `
                --endpoint $hfEndpoint
            if ($LASTEXITCODE -ne 0) { throw 'setup_windows_compute_node_model_sync_failed' }
        }
        finally {
            Stop-Transcript | Out-Null
        }
        Write-PpLog 'models stage complete'
    }
}

# ---------------------------------------------------------------------------
# build stage
# ---------------------------------------------------------------------------
if ($Stage -in @('all', 'build')) {
    $python = Get-PpPython
    if ($Stage -eq 'all') {
        $interactiveUser = Get-PpInteractiveUser
        if (-not $interactiveUser) {
            throw 'setup_windows_compute_node_interactive_user_required'
        }
        $buildArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$setupScript`" -SourceRevision $SourceRevision -ProfilePath `"$ProfilePath`" -Stage build"
        if ($SkipGpuSmoke) { $buildArgs += ' -SkipGpuSmoke' }
        if ($RecreateDedicatedBuilder) { $buildArgs += ' -RecreateDedicatedBuilder' }
        Register-PpTask -Name 'PPNodeBuild' -Execute 'powershell.exe' -Argument $buildArgs `
            -UserId $interactiveUser -LogonType Interactive
        Start-PpTaskOnce -Name 'PPNodeBuild'
        $imageTag = Get-PpValue $profile 'PP_WINDOWS_BUILD_IMAGE_TAG' "plastic-promise-local-inference-node:main-$($SourceRevision.Substring(0, 7))"
        Write-PpLog "image tag: $imageTag (used by the build task)"
    }
    else {
        $log = Join-Path $LogRoot 'node-build.log'
        Start-Transcript -Path $log -Append | Out-Null
        try {
            $userProfile = Get-PpValue $profile 'PP_WINDOWS_USER_PROFILE' $defaultUserProfile
            if (-not $userProfile) {
                throw 'setup_windows_compute_node_user_profile_required'
            }
            $env:PATH = "$(Split-Path -Parent $python);$(Split-Path -Parent $python)\Scripts;$env:PATH"
            $env:USERPROFILE = $userProfile
            $env:HOME = $userProfile
            # The generic one-click build script owns Ollama stop/settle/restart,
            # model identity resolution, and the immutable build; -NoStart keeps
            # service lifecycle with the scheduled-task bootstrap.
            $env:PP_LOCAL_NODE_ID = $nodeId
            $env:PP_LOCAL_NODE_EMBEDDING_BACKEND = $embeddingBackend
            $env:PP_LOCAL_NODE_EMBEDDING_MODEL = $embeddingModel
            $env:PP_LOCAL_NODE_EMBEDDING_DIMENSION = $embeddingDimension
            $env:PP_LOCAL_NODE_EMBEDDING_NORMALIZATION = $embeddingNormalization
            $env:PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE = $embeddingModelReference
            $env:PP_LOCAL_NODE_RERANK_BACKEND = $rerankBackend
            $env:PP_LOCAL_NODE_RERANK_MODEL = $rerankModel
            $env:PP_LOCAL_NODE_RERANK_REVISION = $rerankRevision
            $env:PP_LOCAL_NODE_RERANK_MODEL_REFERENCE = $rerankModelReference
            $env:PP_LOCAL_NODE_OLLAMA_HOST = $nodeOllamaHost
            $env:PP_LOCAL_NODE_MODEL_DIRECTORY = $nodeModelDirectory
            $env:PP_PROXY_URL = $script:proxyUrl
            $oneClickBuild = Join-Path $repoRoot 'scripts\build_compute_node.ps1'
            if (-not (Test-Path -LiteralPath $oneClickBuild)) {
                throw 'setup_windows_compute_node_one_click_build_missing'
            }
            $buildParams = @(
                '-SourceRevision', $SourceRevision,
                '-Variant', 'cuda',
                '-DockerCommand', $script:dockerCommand,
                '-ExecutionMode', (Get-PpValue $profile 'PP_WINDOWS_BUILD_EXECUTION_MODE' (
                    if ($script:dockerCommand -match 'wsl\.exe') { 'wsl' } else { 'native-docker' }
                )),
                '-ImageTag', (Get-PpValue $profile 'PP_WINDOWS_BUILD_IMAGE_TAG' "plastic-promise-local-inference-node:main-$($SourceRevision.Substring(0, 7))"),
                '-Builder', (Get-PpValue $profile 'PP_WINDOWS_BUILD_BUILDER' 'plastic-promise-local'),
                '-RetentionHours', [int](Get-PpValue $profile 'PP_WINDOWS_BUILD_CACHE_RETENTION_HOURS' '24'),
                '-ReportDirectory', (Get-PpValue $profile 'PP_WINDOWS_BUILD_REPORT_DIRECTORY' 'artifacts/local-node-build'),
                '-NodeConfig', (Join-Path $repoRoot 'deploy\local-inference-node\.env'),
                '-NoStart'
            )
            if ($SkipGpuSmoke) { $buildParams += '-SkipGpuSmoke' }
            if ($RecreateDedicatedBuilder) { $buildParams += '-RecreateDedicatedBuilder' }
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $oneClickBuild @buildParams
            if ($LASTEXITCODE -ne 0) { throw "setup_windows_compute_node_build_failed_exit_$LASTEXITCODE" }
        }
        finally {
            Stop-Transcript | Out-Null
        }
        Write-PpLog 'build stage complete'
    }
}

# ---------------------------------------------------------------------------
# env stage
# ---------------------------------------------------------------------------
if ($Stage -in @('all', 'env')) {
    if ($embeddingBackend -eq 'ollama' -and -not $embeddingRevision) {
        if (-not (Test-PpOllamaTags -HostUri 'http://127.0.0.1:11434')) {
            throw 'setup_windows_compute_node_ollama_identity_unavailable'
        }
        $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 8
        $found = $tags.models | Where-Object { $_.name -eq $embeddingModel } | Select-Object -First 1
        if (-not $found) { throw 'setup_windows_compute_node_embedding_model_missing' }
        $embeddingRevision = 'sha256:' + ([string]$found.digest).ToLowerInvariant()
    }
    if (-not $embeddingRevision) {
        throw 'setup_windows_compute_node_embedding_revision_required'
    }
    if (-not (Test-PpRerankTreeComplete -Target $rerankTarget)) {
        Write-PpLog "warning: rerank tree not complete at $rerankTarget; run -Stage models first"
    }
    $composeEnv = Join-Path $repoRoot 'deploy\local-inference-node\.env'
    $composeModelDirectory = if ($script:dockerCommand -match 'wsl\.exe') {
        ConvertTo-PpWslPath -WindowsPath $nodeModelDirectory
    }
    else { $nodeModelDirectory }
    $content = @(
        "PP_LOCAL_NODE_AUTHORIZATION=$nodeAuthorization",
        "PP_LOCAL_NODE_ID=$nodeId",
        "PP_LOCAL_NODE_EMBEDDING_BACKEND=$embeddingBackend",
        "PP_LOCAL_NODE_EMBEDDING_MODEL=$embeddingModel",
        "PP_LOCAL_NODE_EMBEDDING_REVISION=$embeddingRevision",
        "PP_LOCAL_NODE_EMBEDDING_DIMENSION=$embeddingDimension",
        "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION=$embeddingNormalization",
        "PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256=$embeddingArtifactSha256",
        "PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE=$embeddingModelReference",
        "PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL=$(Get-PpValue $profile 'PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL' 'http://127.0.0.1:19131')",
        "PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH=$(Get-PpValue $profile 'PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH' '/v1/embeddings')",
        "PP_LOCAL_NODE_RERANK_BACKEND=$rerankBackend",
        "PP_LOCAL_NODE_RERANK_MODEL=$rerankModel",
        "PP_LOCAL_NODE_RERANK_REVISION=$rerankRevision",
        "PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256=$rerankArtifactSha256",
        "PP_LOCAL_NODE_RERANK_MODEL_REFERENCE=$rerankModelReference",
        "PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL=$(Get-PpValue $profile 'PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL' 'http://127.0.0.1:19132')",
        "PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH=$(Get-PpValue $profile 'PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH' '/rerank')",
        "PP_LOCAL_NODE_OLLAMA_HOST=$nodeOllamaHost",
        "PP_LOCAL_NODE_MODEL_DIRECTORY=$composeModelDirectory"
    ) -join "`r`n"
    [System.IO.File]::WriteAllText($composeEnv, $content + "`r`n", [System.Text.UTF8Encoding]::new($false))
    Protect-PpPrivateFile -Path $composeEnv
    Write-PpLog "wrote compose env: $composeEnv"
    Write-PpLog "embedding identity: $embeddingModel @ $embeddingRevision ($embeddingDimension, $embeddingNormalization)"
    Write-PpLog "rerank identity: $rerankModel @ $rerankRevision"
}

# ---------------------------------------------------------------------------
# verify stage (compose start + full node smoke)
# ---------------------------------------------------------------------------
if ($Stage -eq 'verify') {
    $composeFile = Get-PpValue $profile 'PP_NODE_COMPOSE_FILE' 'deploy\local-inference-node\compose.cuda.yaml'
    $composePath = Join-Path $repoRoot $composeFile
    if (-not (Test-Path -LiteralPath $composePath)) {
        throw "setup_windows_compute_node_compose_missing: $composePath"
    }
    $composeEnv = Join-Path $repoRoot 'deploy\local-inference-node\.env'
    if ($embeddingBackend -eq 'llama.cpp' -and $rerankBackend -eq 'llama.cpp') {
        if (-not (Test-Path -LiteralPath $configureScript -PathType Leaf)) {
            throw 'setup_windows_compute_node_configure_script_missing'
        }
        if ($nodeAuthorization -notmatch '^Bearer [A-Za-z0-9._~+/=-]{1,4096}$') {
            throw 'setup_windows_compute_node_authorization_invalid'
        }
        if (($embeddingArtifactSha256 -notmatch '^sha256:[0-9a-f]{64}$') -or ($rerankArtifactSha256 -notmatch '^sha256:[0-9a-f]{64}$')) {
            throw 'setup_windows_compute_node_artifact_identity_required'
        }
        $composeModelDirectory = if ($script:dockerCommand -match 'wsl\.exe') {
            ConvertTo-PpWslPath -WindowsPath $nodeModelDirectory
        }
        else { $nodeModelDirectory }
        $embeddingFile = ConvertFrom-PpContainerModelReference $embeddingModelReference
        $rerankFile = ConvertFrom-PpContainerModelReference $rerankModelReference
        $authorizationFile = Join-Path $LogRoot ("compute-node-auth-" + [IO.Path]::GetRandomFileName())
        [IO.File]::WriteAllText(
            $authorizationFile,
            $nodeAuthorization + "`r`n",
            [Text.UTF8Encoding]::new($false)
        )
        Protect-PpPrivateFile -Path $authorizationFile
        try {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $configureScript `
                -SourceRoot $repoRoot `
                -AuthorizationFile $authorizationFile `
                -ModelDirectory $composeModelDirectory `
                -NodeId $nodeId `
                -EmbeddingModel $embeddingModel `
                -EmbeddingRevision $embeddingRevision `
                -EmbeddingFile $embeddingFile `
                -EmbeddingDimension ([int]$embeddingDimension) `
                -EmbeddingNormalization $embeddingNormalization `
                -EmbeddingArtifactSha256 $embeddingArtifactSha256 `
                -RerankModel $rerankModel `
                -RerankRevision $rerankRevision `
                -RerankFile $rerankFile `
                -RerankArtifactSha256 $rerankArtifactSha256 `
                -StructuredJsonBackend $structuredJsonBackend
            if ($LASTEXITCODE -ne 0) {
                throw "setup_windows_compute_node_configure_failed_exit_$LASTEXITCODE"
            }
        }
        finally {
            if (Test-Path -LiteralPath $authorizationFile) {
                Remove-Item -LiteralPath $authorizationFile -Force
            }
        }
    }
    $envText = ''
    if (Test-Path -LiteralPath $composeEnv) {
        $envText = Get-Content -LiteralPath $composeEnv -Raw
    }
    if ($envText -notmatch 'PP_BUILD_POLICY_DIGEST=' -or $envText -notmatch 'PP_COMPUTE_CUDA_BASE_IMAGE=') {
        throw 'setup_windows_compute_node_build_identity_required: run -Stage build first (it enriches .env with container identity)'
    }
    Write-PpLog "starting compose node: $composeFile"
    $composeArg = $composePath
    $envArg = $composeEnv
    if ($script:dockerCommand -match 'wsl\.exe') {
        $composeArg = ConvertTo-PpWslPath -WindowsPath $composePath
        $envArg = ConvertTo-PpWslPath -WindowsPath $composeEnv
    }
    $composeUp = Invoke-PpDocker -Arguments @('compose', '-f', $composeArg, '--env-file', $envArg, 'up', '-d', '--no-build')
    if ($composeUp.ExitCode -ne 0) {
        throw 'setup_windows_compute_node_compose_start_failed'
    }
    $containerName = Get-PpValue $profile 'PP_NODE_CONTAINER_NAME' 'local-inference-node-pp-compute-node-1'
    $healthy = $false
    for ($attempt = 0; $attempt -lt 24 -and -not $healthy; $attempt++) {
        Start-Sleep -Seconds 5
        $healthProbe = Invoke-PpDocker -Arguments @('inspect', '--format', '{{.State.Health.Status}}', $containerName)
        $status = $healthProbe.Output
        $healthy = ($status -match 'healthy')
        if (-not $healthy) {
            Write-PpLog "waiting for node health ($status) - attempt $($attempt + 1)/24"
        }
    }
    if (-not $healthy) {
        throw 'setup_windows_compute_node_node_not_healthy'
    }
    $python = Get-PpPython
    $smokeOutput = Join-Path $LogRoot 'node-smoke'
    New-Item -ItemType Directory -Force -Path $smokeOutput | Out-Null
    $baseUrl = Get-PpValue $profile 'PP_NODE_BASE_URL' 'http://127.0.0.1:19130'
    $dockerRuntime = 'desktop'
    if (Test-Path -LiteralPath $preflightReport) {
        try {
            $dockerRuntime = [string](Get-Content -LiteralPath $preflightReport -Raw | ConvertFrom-Json).docker_runtime
        }
        catch { }
    }
    if ($dockerRuntime -eq 'wsl-native') {
        # WSL2 native: the node listener lives on the WSL loopback; run the
        # smoke inside WSL so 127.0.0.1 resolves to the node, not Windows.
        $wslSmoke = ConvertTo-PpWslPath -WindowsPath (Join-Path $repoRoot 'scripts\pp_node_smoke.py')
        $wslEnv = ConvertTo-PpWslPath -WindowsPath $composeEnv
        $wslOut = ConvertTo-PpWslPath -WindowsPath $smokeOutput
        Write-PpLog "running node smoke inside WSL ($wslDistro)"
        & wsl.exe -d $wslDistro -e python3 $wslSmoke `
            --base-url $baseUrl `
            --node-config $wslEnv `
            --expected-dimension $embeddingDimension `
            --expected-normalization $embeddingNormalization `
            --output-dir $wslOut
    }
    else {
        & $python (Join-Path $repoRoot 'scripts\pp_node_smoke.py') `
            --base-url $baseUrl `
            --node-config $composeEnv `
            --expected-dimension $embeddingDimension `
            --expected-normalization $embeddingNormalization `
            --output-dir $smokeOutput
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'setup_windows_compute_node_smoke_failed'
    }
    Write-PpLog "node smoke passed; reports under $smokeOutput"
}

if ($Stage -eq 'all') {
    Write-PpLog 'bootstrap complete. Next:'
    Write-PpLog '  1. wait for PP_NODE_MODEL_SYNC_COMPLETE in <workspace>\logs\model-sync.log'
    Write-PpLog '  2. run/build task PPNodeBuild (resource gate defers while sync is active)'
    Write-PpLog '  3. run the verify stage: setup_windows_compute_node.ps1 ... -Stage verify'
}
