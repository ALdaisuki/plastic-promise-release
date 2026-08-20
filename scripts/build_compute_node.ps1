#Requires -Version 5.1
# One-click local compute-node build, start, and performance smoke (Windows).
#
# Generic across Windows/WSL2 and native Docker Desktop: auto-detects the
# source revision (git HEAD), Docker, CUDA (nvidia-smi), and Ollama, resolves
# the variant, generates a non-secret compose .env with pinned model identity,
# runs the immutable local build (stopping PPOllamaServe for the GPU build and
# restoring it afterwards), starts Compose, and records performance evidence.
# No machine-specific user, path, model, or revision is hard-coded: every value
# is auto-detected or must be supplied through PP_NODE_* environment variables.
[CmdletBinding()]
param(
    [string]$SourceRevision,
    [ValidateSet('auto', 'cpu', 'cuda')]
    [string]$Variant = 'auto',
    [string]$Builder,
    [string]$ImageTag,
    [ValidateRange(1, 2160)]
    [int]$RetentionHours = 24,
    [string]$ReportDirectory,
    [string]$NodeConfig,
    [string]$RuntimeStatus,
    [ValidateSet('auto', 'wsl', 'native-docker')]
    [string]$ExecutionMode = 'auto',
    [ValidateSet('desktop-interactive', 'headless-builder')]
    [string]$CredentialMode = 'desktop-interactive',
    [string]$DockerCommand = 'docker.exe',
    [string]$ProxyUrl = '',
    [switch]$SkipGpuSmoke,
    [switch]$NoStart,
    [switch]$RecreateDedicatedBuilder,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ([string]::IsNullOrWhiteSpace($ProxyUrl)) {
    $ProxyUrl = [string]$env:PP_PROXY_URL
}
if (-not [string]::IsNullOrWhiteSpace($ProxyUrl)) {
    $proxyUri = $null
    if (
        -not [System.Uri]::TryCreate($ProxyUrl, [System.UriKind]::Absolute, [ref]$proxyUri) -or
        $proxyUri.Scheme -notin @('http', 'https') -or
        $proxyUri.UserInfo
    ) {
        throw 'build_compute_node_proxy_url_invalid'
    }
}

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root

foreach ($required in @(
    'scripts/run_windows_local_inference_build.ps1',
    'scripts/pp_node_smoke.py',
    'pyproject.toml'
)) {
    if (!(Test-Path -LiteralPath (Join-Path $root $required))) {
        throw 'build_compute_node_source_root_invalid'
    }
}

function Get-GitHeadRevision {
    $output = (& git.exe rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0) { return '' }
    return ($output -join '').Trim().ToLowerInvariant()
}

function Invoke-PpDocker {
    param([object[]]$Arguments)
    $prefix = if ($DockerCommand -eq 'docker.exe') {
        @('docker.exe')
    }
    elseif ($DockerCommand -match '^wsl\.exe -d ([A-Za-z0-9._-]+) -e docker$') {
        @('wsl.exe', '-d', $matches[1], '-e', 'docker')
    }
    else {
        throw 'build_compute_node_docker_command_invalid'
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

function ConvertTo-PpWslPath {
    param([string]$WindowsPath)
    $drive = $WindowsPath.Substring(0, 1).ToLowerInvariant()
    return '/mnt/' + $drive + '/' + $WindowsPath.Substring(3).Replace('\', '/')
}

function Test-PinnedRevision {
    param([string]$Value)
    return $Value -match '^[0-9a-fA-F]{40}$'
}

if ([string]::IsNullOrWhiteSpace($SourceRevision)) {
    $SourceRevision = Get-GitHeadRevision
}
if (!(Test-PinnedRevision -Value $SourceRevision)) {
    throw 'build_compute_node_source_revision_required'
}
$SourceRevision = $SourceRevision.ToLowerInvariant()
$shortRevision = $SourceRevision.Substring(0, 7)

if ($Variant -eq 'auto') {
    if ($null -ne (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue)) {
        $Variant = 'cuda'
    }
    else {
        $Variant = 'cpu'
    }
}

if ([string]::IsNullOrWhiteSpace($Builder)) { $Builder = 'plastic-promise-local' }
if ([string]::IsNullOrWhiteSpace($ImageTag)) {
    $ImageTag = "plastic-promise-local-inference-node:$Variant-$shortRevision"
}
if ([string]::IsNullOrWhiteSpace($ReportDirectory)) { $ReportDirectory = 'artifacts/local-node-build' }
if ([string]::IsNullOrWhiteSpace($NodeConfig)) { $NodeConfig = 'deploy/local-inference-node/.env' }
$runtimeStatusPath = $RuntimeStatus
if ([string]::IsNullOrWhiteSpace($runtimeStatusPath)) {
    $runtimeStatusPath = Join-Path $ReportDirectory 'runtime-status.json'
}
New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null

function Get-PythonCommand {
    if ($null -ne (Get-Command py.exe -ErrorAction SilentlyContinue)) {
        return @('py.exe', '-3')
    }
    if ($null -ne (Get-Command python.exe -ErrorAction SilentlyContinue)) {
        return @('python.exe')
    }
    throw 'build_compute_node_python_not_available'
}

function Invoke-OllamaProbe {
    param([string]$HostUri)
    try {
        $response = Invoke-RestMethod -Uri "$HostUri/api/tags" -TimeoutSec 10 -ErrorAction Stop
        return $response
    }
    catch {
        return $null
    }
}

# --- resolve model identity (never hard-coded) -----------------------------
$embeddingBackend = $env:PP_LOCAL_NODE_EMBEDDING_BACKEND
$embeddingModel = $env:PP_LOCAL_NODE_EMBEDDING_MODEL
$embeddingRevision = $env:PP_LOCAL_NODE_EMBEDDING_REVISION
$embeddingDimension = $env:PP_LOCAL_NODE_EMBEDDING_DIMENSION
$embeddingNormalization = if ([string]::IsNullOrWhiteSpace($env:PP_LOCAL_NODE_EMBEDDING_NORMALIZATION)) { 'l2' } else { $env:PP_LOCAL_NODE_EMBEDDING_NORMALIZATION }
$rerankBackend = $env:PP_LOCAL_NODE_RERANK_BACKEND
$rerankModel = $env:PP_LOCAL_NODE_RERANK_MODEL
$rerankRevision = $env:PP_LOCAL_NODE_RERANK_REVISION
$ollamaHost = $env:PP_LOCAL_NODE_OLLAMA_HOST
$modelDirectory = $env:PP_LOCAL_NODE_MODEL_DIRECTORY
$ollamaProbeHost = if ([string]::IsNullOrWhiteSpace($env:PP_OLLAMA_PROBE_HOST)) { 'http://127.0.0.1:11434' } else { $env:PP_OLLAMA_PROBE_HOST }

if ([string]::IsNullOrWhiteSpace($embeddingBackend)) {
    $embeddingBackend = 'llama.cpp'
}
if ([string]::IsNullOrWhiteSpace($rerankBackend)) {
    $rerankBackend = 'llama.cpp'
}
if ([string]::IsNullOrWhiteSpace($ollamaHost)) {
    if ($Variant -eq 'cuda') {
        $ollamaHost = 'http://host.docker.internal:11434'
    }
    else {
        $ollamaHost = 'http://127.0.0.1:11434'
    }
}

if ($embeddingBackend -eq 'llama.cpp') {
    if ([string]::IsNullOrWhiteSpace($embeddingModel)) { $embeddingModel = 'Qwen3-Embedding-4B-GGUF' }
    if ([string]::IsNullOrWhiteSpace($embeddingDimension)) { $embeddingDimension = '2560' }
    if ([string]::IsNullOrWhiteSpace($embeddingRevision)) {
        throw 'build_compute_node_llama_cpp_embedding_revision_required'
    }
}
elseif ($embeddingBackend -eq 'ollama') {
    if ([string]::IsNullOrWhiteSpace($embeddingModel)) {
        throw 'build_compute_node_ollama_embedding_model_required'
    }
    if ([string]::IsNullOrWhiteSpace($embeddingRevision)) {
        $tags = Invoke-OllamaProbe -HostUri $ollamaProbeHost
        if ($null -eq $tags) {
            throw 'build_compute_node_ollama_probe_failed'
        }
        $matched = @($tags.models | Where-Object { $_.name -eq $embeddingModel })
        if ($matched.Count -eq 0 -or [string]::IsNullOrWhiteSpace($matched[0].digest)) {
            throw 'build_compute_node_ollama_model_missing'
        }
        $embeddingRevision = [string]$matched[0].digest
    }
    if ([string]::IsNullOrWhiteSpace($embeddingDimension)) {
        $embeddingDimension = '2560'
    }
}
else {
    if ([string]::IsNullOrWhiteSpace($embeddingModel)) { $embeddingModel = 'BAAI/bge-small-en-v1.5' }
    if ([string]::IsNullOrWhiteSpace($embeddingDimension)) { $embeddingDimension = '384' }
    if ([string]::IsNullOrWhiteSpace($embeddingRevision)) {
        throw 'build_compute_node_cpu_embedding_revision_required'
    }
}

if ($rerankBackend -eq 'llama.cpp') {
    if ([string]::IsNullOrWhiteSpace($rerankModel)) { $rerankModel = 'Qwen3-Reranker-0.6B-GGUF' }
    if ([string]::IsNullOrWhiteSpace($rerankRevision)) {
        throw 'build_compute_node_llama_cpp_rerank_revision_required'
    }
}
elseif ($rerankBackend -eq 'qwen3-cross-encoder') {
    if ([string]::IsNullOrWhiteSpace($rerankModel)) { $rerankModel = 'Qwen/Qwen3-Reranker-4B' }
    if ([string]::IsNullOrWhiteSpace($rerankRevision)) {
        throw 'build_compute_node_rerank_revision_required'
    }
}
else {
    if ([string]::IsNullOrWhiteSpace($rerankModel)) { $rerankModel = 'BAAI/bge-reranker-v2-m3' }
    if ([string]::IsNullOrWhiteSpace($rerankRevision)) {
        throw 'build_compute_node_cpu_rerank_revision_required'
    }
}
if ([string]::IsNullOrWhiteSpace($modelDirectory)) {
    throw 'build_compute_node_model_directory_required'
}
if (!(Test-Path -LiteralPath $modelDirectory)) {
    throw 'build_compute_node_model_directory_missing'
}

$nodeId = if ([string]::IsNullOrWhiteSpace($env:PP_LOCAL_NODE_ID)) { 'inference-node' } else { $env:PP_LOCAL_NODE_ID }
$composeModelDirectory = if ($DockerCommand -match 'wsl\.exe') {
    ConvertTo-PpWslPath -WindowsPath $modelDirectory
}
else { $modelDirectory }
$envContent = @(
    "PP_LOCAL_NODE_ID=$nodeId",
    "PP_LOCAL_NODE_EMBEDDING_BACKEND=$embeddingBackend",
    "PP_LOCAL_NODE_EMBEDDING_MODEL=$embeddingModel",
    "PP_LOCAL_NODE_EMBEDDING_REVISION=$embeddingRevision",
    "PP_LOCAL_NODE_EMBEDDING_DIMENSION=$embeddingDimension",
    "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION=$embeddingNormalization",
    "PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE=$(if ($env:PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE) { $env:PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE } else { '/models/embedding' })",
    "PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL=$(if ($env:PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL) { $env:PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL } else { 'http://127.0.0.1:19131' })",
    "PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH=$(if ($env:PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH) { $env:PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH } else { '/v1/embeddings' })",
    "PP_LOCAL_NODE_RERANK_BACKEND=$rerankBackend",
    "PP_LOCAL_NODE_RERANK_MODEL=$rerankModel",
    "PP_LOCAL_NODE_RERANK_REVISION=$rerankRevision",
    "PP_LOCAL_NODE_RERANK_MODEL_REFERENCE=$(if ($env:PP_LOCAL_NODE_RERANK_MODEL_REFERENCE) { $env:PP_LOCAL_NODE_RERANK_MODEL_REFERENCE } else { '/models/rerank' })",
    "PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL=$(if ($env:PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL) { $env:PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL } else { 'http://127.0.0.1:19132' })",
    "PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH=$(if ($env:PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH) { $env:PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH } else { '/rerank' })",
    "PP_LOCAL_NODE_OLLAMA_HOST=$ollamaHost",
    "PP_LOCAL_NODE_MODEL_DIRECTORY=$composeModelDirectory"
) -join "`n"
[System.IO.File]::WriteAllText(
    (Join-Path $root $NodeConfig),
    "$envContent`n",
    [System.Text.UTF8Encoding]::new($false)
)
Write-Output "wrote node compose env: $NodeConfig"

# --- immutable build (Ollama stopped for exclusive GPU build) ---------------
$buildArgs = @(
    '-SourceRevision', $SourceRevision,
    '-ComputeVariant', $Variant,
    '-RepositoryRoot', $root,
    '-DockerCommand', $DockerCommand,
    '-ProxyUrl', $ProxyUrl,
    '-Builder', $Builder,
    '-ImageTag', $ImageTag,
    '-RetentionHours', $RetentionHours,
    '-ReportDirectory', $ReportDirectory,
    '-ExecutionMode', $ExecutionMode,
    '-CredentialMode', $CredentialMode
)
if ($SkipGpuSmoke) { $buildArgs += '-SkipGpuSmoke' }
if ($RecreateDedicatedBuilder) { $buildArgs += '-RecreateDedicatedBuilder' }

if ($DryRun) {
    Write-Output "build: powershell -File scripts/run_windows_local_inference_build.ps1 $($buildArgs -join ' ')"
}
else {
    $restartOllama = $false
    $ollamaTask = Get-ScheduledTask -TaskName 'PPOllamaServe' -ErrorAction SilentlyContinue
    if ($Variant -eq 'cuda' -and $ollamaTask -and $ollamaTask.State -eq 'Running') {
        Stop-ScheduledTask -TaskName 'PPOllamaServe'
        $restartOllama = $true
        Write-Output 'stopped PPOllamaServe for exclusive GPU build'
        $settleSeconds = 30
        if (-not [string]::IsNullOrWhiteSpace($env:PP_BUILD_OLLAMA_SETTLE_SECONDS)) {
            $settleSeconds = [int]$env:PP_BUILD_OLLAMA_SETTLE_SECONDS
        }
        Write-Output "waiting ${settleSeconds}s for memory reclaim before resource gate"
        Start-Sleep -Seconds $settleSeconds
    }
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts/run_windows_local_inference_build.ps1') @buildArgs
        if ($LASTEXITCODE -ne 0) {
            throw "build_compute_node_build_failed_exit_$LASTEXITCODE"
        }
    }
    finally {
        if ($restartOllama) {
            Start-ScheduledTask -TaskName 'PPOllamaServe'
            $ready = $false
            for ($attempt = 0; $attempt -lt 30 -and -not $ready; $attempt++) {
                Start-Sleep -Seconds 2
                $ready = $null -ne (Invoke-OllamaProbe -HostUri $ollamaProbeHost)
            }
            Write-Output "PPOllamaServe restarted (tags_ready=$ready)"
        }
    }
}

$composeFile = if ($Variant -eq 'cpu') {
    'deploy/local-inference-node/compose.cpu.yaml'
}
else {
    'deploy/local-inference-node/compose.cuda.yaml'
}

if ($DryRun) {
    Write-Output "start: docker compose -f $composeFile --env-file $NodeConfig up -d --no-build"
    Write-Output "smoke: py -3 scripts/pp_node_smoke.py --node-config $NodeConfig --output-dir $ReportDirectory --runtime-status $runtimeStatusPath"
    exit 0
}

function Get-ComposeImage {
    param([string]$File)
    $match = Select-String -LiteralPath (Join-Path $root $File) -Pattern '^\s*image:\s*(\S+)\s*$' |
        Select-Object -First 1
    if ($null -eq $match) { throw 'build_compute_node_compose_image_missing' }
    return $match.Matches[0].Groups[1].Value
}

function Test-ImageTritonDeps {
    param([string]$Image)
    $check = Invoke-PpDocker -Arguments @('run', '--rm', '--entrypoint', 'sh', $Image, '-c', "find /usr/include -path '*/Python.h' -print -quit | grep -q . && command -v gcc >/dev/null 2>&1 && command -v g++ >/dev/null 2>&1 && echo DEPS_OK || echo DEPS_MISSING")
    return ($check.ExitCode -eq 0 -and $check.Output -match 'DEPS_OK')
}

function Repair-ImageTritonDeps {
    param([string]$Image, [string]$ProxyUrl)
    $repaired = "$Image-triton-fixed"
    $overlayDir = Join-Path $env:TEMP 'pp-node-overlay'
    New-Item -ItemType Directory -Force -Path $overlayDir | Out-Null
    $dockerfile = @(
        "FROM $Image",
        'USER root',
        'RUN apt-get update && apt-get install -y --no-install-recommends python3-dev gcc g++ && rm -rf /var/lib/apt/lists/*',
        'USER ppnode'
    ) -join "`n"
    $dfPath = Join-Path $overlayDir 'Dockerfile.triton-deps'
    [System.IO.File]::WriteAllText($dfPath, "$dockerfile`n", [System.Text.UTF8Encoding]::new($false))
    $buildFile = $dfPath
    $buildContext = $overlayDir
    if ($DockerCommand -match 'wsl\.exe') {
        $buildFile = ConvertTo-PpWslPath -WindowsPath $dfPath
        $buildContext = ConvertTo-PpWslPath -WindowsPath $overlayDir
    }
    $buildArgs = @('build', '--network=host', '-f', $buildFile, '-t', $repaired)
    if (-not [string]::IsNullOrWhiteSpace($ProxyUrl)) {
        $buildArgs += @(
            '--build-arg', "HTTP_PROXY=$ProxyUrl",
            '--build-arg', "HTTPS_PROXY=$ProxyUrl",
            '--build-arg', "http_proxy=$ProxyUrl",
            '--build-arg', "https_proxy=$ProxyUrl"
        )
    }
    $buildArgs += $buildContext
    Write-Output "repairing image Triton deps via overlay: $repaired"
    $repair = Invoke-PpDocker -Arguments $buildArgs
    if ($repair.ExitCode -ne 0) {
        throw 'build_compute_node_image_repair_failed'
    }
    return $repaired
}

# --- bridge container identity into the compose env (fail closed) -----------
$identityFile = Get-ChildItem -LiteralPath (Join-Path $root $ReportDirectory) `
    -Filter 'container-build-identity-*.json' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($null -eq $identityFile) {
    throw 'build_compute_node_container_identity_missing'
}
$identity = Get-Content -LiteralPath $identityFile.FullName -Raw | ConvertFrom-Json
$buildArgs = $identity.build_args
$basePrefix = if ($Variant -eq 'cpu') { 'PP_COMPUTE_CPU' } else { 'PP_COMPUTE_CUDA' }
$identityEnv = [ordered]@{
    "${basePrefix}_BASE_IMAGE" = [string]$identity.base_image_reference
    "${basePrefix}_BASE_IMAGE_DIGEST" = [string]$identity.base_image_digest
    'PP_BUILD_SOURCE_REVISION' = [string]$buildArgs.SOURCE_REVISION
    'PP_BUILD_PACKAGE_VERSION' = [string]$buildArgs.PACKAGE_VERSION
    'PP_BUILD_POLICY_DIGEST' = [string]$buildArgs.BUILD_POLICY_DIGEST
    'PP_RECIPE_POLICY_DIGEST' = [string]$buildArgs.RECIPE_POLICY_DIGEST
}
$envPath = Join-Path $root $NodeConfig
$envValues = @{}
foreach ($line in (Get-Content -LiteralPath $envPath)) {
    if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        $envValues[$matches[1]] = $matches[2]
    }
}
foreach ($key in $identityEnv.Keys) {
    $envValues[$key] = $identityEnv[$key]
}
$envContent = ($envValues.GetEnumerator() | Sort-Object Name | ForEach-Object {
    "$($_.Key)=$($_.Value)"
}) -join "`r`n"
[System.IO.File]::WriteAllText($envPath, "$envContent`r`n", [System.Text.UTF8Encoding]::new($false))
Write-Output "enriched compose env with container identity: $($identityEnv.Keys -join ', ')"

# --- ensure Triton JIT dependencies (Python.h + gcc) before aliasing -------
$composeImage = Get-ComposeImage -File $composeFile
if (-not (Test-ImageTritonDeps -Image $ImageTag)) {
    Write-Output 'built image missing Triton JIT dependencies; repairing with overlay layer'
    $ImageTag = Repair-ImageTritonDeps -Image $ImageTag -ProxyUrl $ProxyUrl
    if (-not (Test-ImageTritonDeps -Image $ImageTag)) {
        throw 'build_compute_node_image_repair_verify_failed'
    }
}
$alias = Invoke-PpDocker -Arguments @('tag', $ImageTag, $composeImage)
if ($alias.ExitCode -ne 0) {
    throw 'build_compute_node_image_alias_failed'
}
Write-Output "aliased built image $ImageTag -> $composeImage"

if ($NoStart) {
    Write-Output 'no-start requested; image repaired/aliased and identity env written without Compose start'
    exit 0
}

$composeFileArg = $composeFile
$composeEnvArg = $envPath
if ($DockerCommand -match 'wsl\.exe') {
    $composeFileArg = ConvertTo-PpWslPath -WindowsPath (Join-Path $root $composeFile)
    $composeEnvArg = ConvertTo-PpWslPath -WindowsPath $envPath
}
$composeUp = Invoke-PpDocker -Arguments @('compose', '-f', $composeFileArg, '--env-file', $composeEnvArg, 'up', '-d', '--no-build')
if ($composeUp.ExitCode -ne 0) {
    throw 'build_compute_node_compose_start_failed'
}
$python = Get-PythonCommand
& $python[0] @($python | Select-Object -Skip 1) scripts/pp_node_smoke.py `
    --node-config (Join-Path $root $NodeConfig) `
    --output-dir $ReportDirectory `
    --runtime-status $runtimeStatusPath
if ($LASTEXITCODE -ne 0) {
    throw 'build_compute_node_smoke_failed'
}
Write-Output "one-click compute-node build/start/smoke complete; reports under $ReportDirectory"
