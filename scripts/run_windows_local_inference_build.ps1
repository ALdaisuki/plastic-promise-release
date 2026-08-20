[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$SourceRevision,

    [ValidateSet('auto', 'cpu', 'cuda')]
    [string]$ComputeVariant = 'auto',

    [string]$RepositoryRoot = '',

    [string]$Distro = 'Ubuntu-22.04',

    [ValidateSet('auto', 'wsl', 'native-docker')]
    [string]$ExecutionMode = 'auto',

    [ValidateSet('desktop-interactive', 'headless-builder')]
    [string]$CredentialMode = 'desktop-interactive',

    [string]$DockerCommand = 'docker.exe',

    [string]$ProxyUrl = '',

    [string]$ImageTag = 'plastic-promise-local-inference-node:local',

    [ValidatePattern('^https?://[^ ]+$')]
    [string]$PipIndexUrl = 'https://pypi.org/simple',

    [string]$Builder = 'plastic-promise-local',

    [ValidatePattern('^moby/buildkit@sha256:[0-9a-fA-F]{64}$')]
    [string]$BuildkitImage = 'moby/buildkit@sha256:2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec',

    [ValidateRange(1, 5)]
    [int]$BuildkitPullAttempts = 3,

    [ValidateRange(1, 5)]
    [int]$BuildAttempts = 3,

    [string]$BuildxConfigDirectory = '',

    [ValidatePattern('^[a-zA-Z0-9.-]+(?::[0-9]+)?$')]
    [string]$DockerHubMirror = 'mirror.gcr.io',

    [string]$BuildkitConfigDirectory = '',

    [ValidateRange(1, 2160)]
    [int]$RetentionHours = 24,

    [string]$ReportDirectory = 'artifacts/local-node-build',

    [switch]$RecreateDedicatedBuilder,

    [switch]$SkipGpuSmoke
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

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
        throw 'windows_local_build_proxy_url_invalid'
    }
    $env:HTTP_PROXY = $ProxyUrl
    $env:HTTPS_PROXY = $ProxyUrl
    $env:http_proxy = $ProxyUrl
    $env:https_proxy = $ProxyUrl
}

function Get-WindowsPythonCommand {
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        return @('py.exe', '-3')
    }
    if (Get-Command python.exe -ErrorAction SilentlyContinue) {
        return @('python.exe')
    }
    throw 'windows_local_build_python_not_available'
}

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent $PSScriptRoot
}
$resolvedRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$SourceRevision = $SourceRevision.ToLowerInvariant()
$null = Set-Location -LiteralPath $resolvedRoot
if ([string]::IsNullOrWhiteSpace($BuildxConfigDirectory)) {
    $BuildxConfigDirectory = Join-Path $env:TEMP 'plastic-promise-buildx'
}
if ([string]::IsNullOrWhiteSpace($BuildkitConfigDirectory)) {
    $BuildkitConfigDirectory = Join-Path $env:TEMP 'plastic-promise-buildkit'
}
if ($ComputeVariant -eq 'auto') {
    if ($null -ne (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue)) {
        $ComputeVariant = 'cuda'
    }
    else {
        $ComputeVariant = 'cpu'
    }
}
$python = Get-WindowsPythonCommand
$pythonExecutable = $python[0]
$pythonPrefix = @($python | Select-Object -Skip 1)
# Docker invocation abstraction: Docker Desktop uses docker.exe; WSL2 native
# daemons use the WSL command prefix ("wsl.exe -d <distro> -e docker"), which
# works on NAT and mirrored networking without relying on localhost bridging.
$script:dockerCmd = if ($DockerCommand -eq 'docker.exe') {
    @('docker.exe')
}
elseif ($DockerCommand -match '^wsl\.exe -d ([A-Za-z0-9._-]+) -e docker$') {
    @('wsl.exe', '-d', $matches[1], '-e', 'docker')
}
else {
    throw 'windows_local_build_docker_command_invalid'
}
$dockerCommandName = $script:dockerCmd[0]
if (-not (Get-Command $dockerCommandName -ErrorAction SilentlyContinue)) {
    throw "windows_local_build_docker_command_not_available: $DockerCommand"
}
function Invoke-PpDocker {
    param([object[]]$Arguments)
    $prefix = $script:dockerCmd
    $command = $prefix[0]
    $prefixArgs = @($prefix | Select-Object -Skip 1)
    $effective = @($prefixArgs)
    if ($script:dockerCmd[0] -eq 'docker.exe') {
        $effective += $dockerConfigArguments
    }
    $effective += $Arguments
    # PS 5.1 promotes native stderr into terminating errors under
    # $ErrorActionPreference=Stop; capture both streams and return structured
    # results so callers can inspect exit codes and output safely.
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

# Validate the exact SHA-scoped D: workspace before any Docker inspection,
# builder reset, cleanup, or model/image side effect.
& $pythonExecutable @pythonPrefix -m plastic_promise.release_builder.resource_probe `
    validate-windows-source `
    --path $resolvedRoot `
    --source-revision $SourceRevision
if ($LASTEXITCODE -ne 0) {
    throw 'windows_local_build_source_workspace_invalid'
}
if (-not (Test-Path -LiteralPath (Join-Path $resolvedRoot 'deploy/local-inference-node/Dockerfile'))) {
    throw 'windows_local_build_source_root_invalid'
}
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'windows_local_build_wsl_not_available'
}
$previousDockerConfig = $env:DOCKER_CONFIG
$previousBuildxConfig = $env:BUILDX_CONFIG
$dockerConfigDirectory = $null
$dockerConfigArguments = @()

function Restore-DockerCredentialContext {
    if ($null -eq $previousDockerConfig) {
        Remove-Item Env:DOCKER_CONFIG -ErrorAction SilentlyContinue
    }
    else {
        $env:DOCKER_CONFIG = $previousDockerConfig
    }
    if ($null -eq $previousBuildxConfig) {
        Remove-Item Env:BUILDX_CONFIG -ErrorAction SilentlyContinue
    }
    else {
        $env:BUILDX_CONFIG = $previousBuildxConfig
    }
    if (
        $null -ne $dockerConfigDirectory -and
        (Test-Path -LiteralPath $dockerConfigDirectory)
    ) {
        Remove-Item -LiteralPath $dockerConfigDirectory -Recurse -Force
    }
}

if ($CredentialMode -eq 'headless-builder') {
    # Each SSH/headless invocation gets a new empty Docker configuration.
    # Never reuse the report directory: a stale config there could re-enable
    # credsStore, credHelpers, or saved registry authentication.
    $dockerConfigDirectory = Join-Path (
        [System.IO.Path]::GetTempPath()
    ) "plastic-promise-docker-config-$([Guid]::NewGuid().ToString('N'))"
    try {
        New-Item -ItemType Directory -Path $dockerConfigDirectory | Out-Null
        [System.IO.File]::WriteAllText(
            (Join-Path $dockerConfigDirectory 'config.json'),
            "{`"auths`":{`"https://index.docker.io/v1/`":{},`"registry-1.docker.io`":{}},`"credsStore`":`"`",`"credHelpers`":{}}`n",
            [System.Text.UTF8Encoding]::new($false)
        )
        $env:DOCKER_CONFIG = $dockerConfigDirectory
        New-Item -ItemType Directory -Path $BuildxConfigDirectory -Force | Out-Null
        $env:BUILDX_CONFIG = $BuildxConfigDirectory
        $dockerConfigArguments = @('--config', $dockerConfigDirectory)
    }
    catch {
        Restore-DockerCredentialContext
        throw
    }
}

function Test-DockerImagePresent {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Image
    )

    $probe = Invoke-PpDocker -Arguments @('image', 'inspect', $Image)
    return $probe.ExitCode -eq 0
}

function Invoke-DockerPullWithRetry {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Image,

        [Parameter(Mandatory = $true)]
        [ValidateRange(1, 5)]
        [int]$Attempts
    )

    if (Test-DockerImagePresent -Image $Image) {
        Write-Output "Using cached immutable image: $Image"
        return
    }
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $pull = Invoke-PpDocker -Arguments @('pull', $Image)
        if ($pull.ExitCode -eq 0) {
            return
        }
        if ($attempt -lt $Attempts) {
            Start-Sleep -Seconds ([Math]::Min(10, 2 * $attempt))
        }
    }
    throw 'windows_local_build_buildkit_pull_failed'
}

function Invoke-DockerBuildWithRetry {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$BuildArguments,

        [Parameter(Mandatory = $true)]
        [ValidateRange(1, 5)]
        [int]$Attempts
    )

    $lastBuildExitCode = 1
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $build = Invoke-PpDocker -Arguments $BuildArguments
        $lastBuildExitCode = $build.ExitCode
        if ($lastBuildExitCode -eq 0) {
            return
        }
        if ($attempt -lt $Attempts) {
            Write-Warning "Docker build attempt $attempt failed; retrying the build phase."
            Start-Sleep -Seconds ([Math]::Min(15, 5 * $attempt))
        }
    }
    throw "windows_local_build_failed_exit_$lastBuildExitCode"
}

function Initialize-BuildkitRegistryConfig {
    New-Item -ItemType Directory -Path $BuildkitConfigDirectory -Force | Out-Null
    $configPath = Join-Path $BuildkitConfigDirectory 'buildkitd.toml'
    $config = @"
[registry."docker.io"]
  mirrors = ["$DockerHubMirror"]
"@
    [System.IO.File]::WriteAllText(
        $configPath,
        "$config`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    return $configPath
}
# Docker Desktop inspection is read-only. The selected build path performs its
# mandatory 10-second observation immediately before its first Docker mutation.
$versionProbe = Invoke-PpDocker -Arguments @('version', '--format', '{{.Server.Version}}')
if ($versionProbe.ExitCode -ne 0) {
    Restore-DockerCredentialContext
    throw 'windows_local_build_docker_unavailable'
}

# A WSL-prefixed docker command can only drive the WSL build path; the
# native-docker path depends on a Windows docker.exe that reaches a daemon.
if ($script:dockerCmd[0] -ne 'docker.exe' -and $ExecutionMode -eq 'native-docker') {
    Write-Output 'docker command is WSL-prefixed; forcing ExecutionMode=wsl'
    $ExecutionMode = 'wsl'
}

function Invoke-WslBuild {
    $wslRoot = (& wsl.exe --distribution $Distro -- wslpath -a $resolvedRoot 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($wslRoot)) {
        return $false
    }

    $arguments = @(
        '--distribution', $Distro,
        '--cd', $wslRoot,
        '--', 'env'
    )
    if (-not [string]::IsNullOrWhiteSpace($ProxyUrl)) {
        $arguments += @(
            "HTTP_PROXY=$ProxyUrl",
            "HTTPS_PROXY=$ProxyUrl",
            "http_proxy=$ProxyUrl",
            "https_proxy=$ProxyUrl"
        )
    }
    $arguments += @(
        'bash', 'scripts/run_local_inference_node_build.sh',
        '--source-revision', $SourceRevision,
        '--compute-variant', $ComputeVariant,
        '--image-tag', $ImageTag,
        '--builder', $Builder,
        '--retention-hours', $RetentionHours,
        '--report-directory', $ReportDirectory,
        '--credential-mode', $CredentialMode,
        '--pip-index-url', $PipIndexUrl,
        '--windows-source-root', $resolvedRoot,
        '--disk-path', $wslRoot
    )
    if ($SkipGpuSmoke) {
        $arguments += '--skip-gpu-smoke'
    }

    & wsl.exe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "windows_local_build_wsl_failed_exit_$LASTEXITCODE"
    }
    return $true
}

function Invoke-NativeDockerBuild {
    # A 75 exit means deferred_resource_busy and deliberately creates no
    # builder, cache-cleanup report, image, container, queue item, or retry.
    & $pythonExecutable @pythonPrefix -m plastic_promise.release_builder.resource_probe `
        resource-gate `
        --disk-path $resolvedRoot
    if ($LASTEXITCODE -eq 75) {
        throw 'deferred_resource_busy'
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'windows_local_build_resource_gate_failed'
    }
    $buildkitConfigPath = Initialize-BuildkitRegistryConfig
    Invoke-DockerPullWithRetry -Image $BuildkitImage -Attempts $BuildkitPullAttempts
    $reportPath = Join-Path $resolvedRoot $ReportDirectory
    New-Item -ItemType Directory -Path $reportPath -Force | Out-Null
    $timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $cleanupReport = Join-Path $reportPath "docker-cleanup-$timestamp.jsonl"
    $buildReport = Join-Path $reportPath "local-inference-build-$timestamp.json"
    $builderList = Invoke-PpDocker -Arguments @('buildx', 'ls', '--format', '{{.Name}}')
    if ($builderList.ExitCode -ne 0) {
        throw 'windows_local_build_builder_list_failed'
    }
    $builders = @($builderList.Output -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($RecreateDedicatedBuilder -and $builders -contains $Builder) {
        $rm = Invoke-PpDocker -Arguments @('buildx', 'rm', $Builder)
        if ($rm.ExitCode -ne 0) {
            throw 'windows_local_build_builder_recreate_remove_failed'
        }
        $builderList2 = Invoke-PpDocker -Arguments @('buildx', 'ls', '--format', '{{.Name}}')
        if ($builderList2.ExitCode -ne 0) {
            throw 'windows_local_build_builder_list_failed'
        }
        $builders = @($builderList2.Output -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
    if ($builders -notcontains $Builder) {
        $create = Invoke-PpDocker -Arguments @('buildx', 'create', '--name', $Builder, '--driver', 'docker-container', '--driver-opt', "image=$BuildkitImage", '--buildkitd-config', $buildkitConfigPath)
        if ($create.ExitCode -ne 0) {
            throw 'windows_local_build_builder_create_failed'
        }
    }
    $inspect = Invoke-PpDocker -Arguments @('buildx', 'inspect', $Builder, '--bootstrap')
    if ($inspect.ExitCode -ne 0) {
        throw 'windows_local_build_builder_bootstrap_failed'
    }

    # The cleanup implementation records its exact plan before its first
    # mutable Docker command and cannot cross the Plastic Promise image/cache
    # boundary. This call is mandatory before every native Windows build.
    $cleanupArguments = @(
        'scripts/prepare_oci_build.py', '--execute',
        '--builder', $Builder,
        '--retention-hours', $RetentionHours,
        '--report', $cleanupReport
    )
    if ($CredentialMode -eq 'headless-builder') {
        $cleanupArguments += @('--docker-config', $dockerConfigDirectory)
    }
    & $pythonExecutable @pythonPrefix @cleanupArguments
    if ($LASTEXITCODE -ne 0) {
        throw "windows_local_build_cleanup_failed_exit_$LASTEXITCODE"
    }

    $pyprojectContent = [System.IO.File]::ReadAllText(
        (Join-Path $resolvedRoot 'pyproject.toml'),
        [System.Text.UTF8Encoding]::new($false)
    )
    $versionMatch = [regex]::Match(
        $pyprojectContent,
        '(?m)^version\s*=\s*"([^"]+)"\s*$'
    )
    if (-not $versionMatch.Success) {
        throw 'windows_local_build_package_version_unreadable'
    }
    $packageVersion = $versionMatch.Groups[1].Value

    $localCatalogBytes = [System.Text.Encoding]::UTF8.GetBytes(
        'plastic-promise-local-builder-catalog/v1'
    )
    $localCatalogHasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $localCatalogDigest = 'sha256:' + (
            [System.BitConverter]::ToString(
                $localCatalogHasher.ComputeHash($localCatalogBytes)
            ).Replace('-', '').ToLowerInvariant()
        )
    }
    finally {
        $localCatalogHasher.Dispose()
    }

    # The resolver is the only source for the Docker build identity. It checks
    # the exact clean source revision, static recipes, and versioned base-image
    # catalog before Docker receives a Dockerfile build command.
    $identityFile = Join-Path $reportPath "container-build-identity-$timestamp.json"
    $identityArguments = @(
        'scripts/resolve_container_artifact_identity.py',
        '--repository-root', $resolvedRoot,
        '--profile-id', 'split-accelerated',
        '--source-revision', $SourceRevision,
        '--package-version', $packageVersion,
        '--platform', 'linux/amd64',
        '--compute-variant', $ComputeVariant,
        '--model-catalog-reference', 'local-builder-catalog',
        '--model-catalog-digest', $localCatalogDigest,
        '--artifact-role', 'pp-compute-node',
        '--artifact-platform', 'linux/amd64',
        '--artifact-variant', $ComputeVariant,
        '--verify-head',
        '--output', $identityFile
    )
    & $pythonExecutable @pythonPrefix @identityArguments | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $identityFile)) {
        throw 'windows_local_build_container_identity_resolution_failed'
    }
    try {
        $identity = Get-Content -LiteralPath $identityFile -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw 'windows_local_build_container_identity_output_invalid'
    }
    if (
        $identity.schema_version -ne 'plastic-promise-container-build-identity/v1' -or
        $identity.artifact_id -ne "compute-node-linux-amd64-$ComputeVariant" -or
        $identity.role -ne 'pp-compute-node' -or
        $identity.platform -ne 'linux/amd64' -or
        $identity.variant -ne $ComputeVariant
    ) {
        throw 'windows_local_build_container_identity_output_invalid'
    }

    function Get-ContainerIdentityBuildArgument {
        param(
            [Parameter(Mandatory = $true)]
            [object]$BuildArgs,

            [Parameter(Mandatory = $true)]
            [string]$Name
        )

        $property = $BuildArgs.PSObject.Properties[$Name]
        if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
            throw 'windows_local_build_container_identity_output_invalid'
        }
        return [string]$property.Value
    }

    $baseImage = Get-ContainerIdentityBuildArgument -BuildArgs $identity.build_args -Name 'BASE_IMAGE'
    $baseImageDigest = Get-ContainerIdentityBuildArgument `
        -BuildArgs $identity.build_args -Name 'BASE_IMAGE_DIGEST'
    $computeVariant = Get-ContainerIdentityBuildArgument `
        -BuildArgs $identity.build_args -Name 'COMPUTE_VARIANT'
    $resolvedSourceRevision = Get-ContainerIdentityBuildArgument `
        -BuildArgs $identity.build_args -Name 'SOURCE_REVISION'
    $resolvedPackageVersion = Get-ContainerIdentityBuildArgument `
        -BuildArgs $identity.build_args -Name 'PACKAGE_VERSION'
    $buildPolicyDigest = Get-ContainerIdentityBuildArgument `
        -BuildArgs $identity.build_args -Name 'BUILD_POLICY_DIGEST'
    $recipePolicyDigest = Get-ContainerIdentityBuildArgument `
        -BuildArgs $identity.build_args -Name 'RECIPE_POLICY_DIGEST'
    if (
        $resolvedSourceRevision -ne $SourceRevision -or
        $resolvedPackageVersion -ne $packageVersion -or
        $computeVariant -ne $ComputeVariant -or
        $baseImage -notmatch '@sha256:[0-9a-f]{64}$' -or
        $baseImageDigest -notmatch '^sha256:[0-9a-f]{64}$' -or
        $baseImage -notlike "*@$baseImageDigest" -or
        $buildPolicyDigest -notmatch '^sha256:[0-9a-f]{64}$' -or
        $recipePolicyDigest -notmatch '^sha256:[0-9a-f]{64}$'
    ) {
        throw 'windows_local_build_container_identity_output_invalid'
    }

    $buildArguments = @(
        'buildx', 'build',
        '--builder', $Builder,
        '--load',
        '--platform', 'linux/amd64',
        '--file', 'deploy/local-inference-node/Dockerfile',
        '--tag', $ImageTag,
        '--build-arg', "BASE_IMAGE=$baseImage",
        '--build-arg', "BASE_IMAGE_DIGEST=$baseImageDigest",
        '--build-arg', "COMPUTE_VARIANT=$computeVariant",
        '--build-arg', "SOURCE_REVISION=$resolvedSourceRevision",
        '--build-arg', "PACKAGE_VERSION=$resolvedPackageVersion",
        '--build-arg', "BUILD_POLICY_DIGEST=$buildPolicyDigest",
        '--build-arg', "RECIPE_POLICY_DIGEST=$recipePolicyDigest",
        '--build-arg', "PIP_INDEX_URL=$PipIndexUrl"
    )
    if (-not [string]::IsNullOrWhiteSpace($ProxyUrl)) {
        $buildArguments += @(
            '--build-arg', "HTTP_PROXY=$ProxyUrl",
            '--build-arg', "HTTPS_PROXY=$ProxyUrl",
            '--build-arg', "http_proxy=$ProxyUrl",
            '--build-arg', "https_proxy=$ProxyUrl"
        )
    }
    $buildArguments += $resolvedRoot
    Invoke-DockerBuildWithRetry -BuildArguments $buildArguments -Attempts $BuildAttempts

    $labelsProbe = Invoke-PpDocker -Arguments @('image', 'inspect', '--format', '{{json .Config.Labels}}', $ImageTag)
    if ($labelsProbe.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($labelsProbe.Output)) {
        throw 'windows_local_build_image_identity_mismatch'
    }
    $labelsJson = $labelsProbe.Output.Trim()
    try {
        $imageLabels = $labelsJson | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw 'windows_local_build_image_identity_mismatch'
    }
    $expectedLabels = [ordered]@{
        'org.opencontainers.image.revision' = $resolvedSourceRevision
        'org.opencontainers.image.base.name' = $baseImage
        'org.opencontainers.image.base.digest' = $baseImageDigest
        'org.plastic-promise.build.policy-digest' = $buildPolicyDigest
        'org.plastic-promise.build.recipe-policy-digest' = $recipePolicyDigest
    }
    foreach ($labelName in $expectedLabels.Keys) {
        $actualProperty = $imageLabels.PSObject.Properties[$labelName]
        $actualValue = if ($null -eq $actualProperty) { $null } else { [string]$actualProperty.Value }
        if ($actualValue -ne $expectedLabels[$labelName]) {
            throw 'windows_local_build_image_identity_mismatch'
        }
    }

    $pkgSmoke = Invoke-PpDocker -Arguments @('run', '--rm', '--entrypoint', 'plastic-promise-local-inference-node', $ImageTag, '--help')
    if ($pkgSmoke.ExitCode -ne 0) {
        throw 'windows_local_build_package_smoke_failed'
    }

    $gpuSmoke = 'not_applicable_cpu_variant'
    if (-not $SkipGpuSmoke -and $ComputeVariant -eq 'cuda') {
        $gpuProbe = Invoke-PpDocker -Arguments @('run', '--rm', '--gpus', 'all', '--entrypoint', 'nvidia-smi', $ImageTag, '--query-gpu=name,driver_version,memory.total', '--format=csv,noheader')
        if ($gpuProbe.ExitCode -ne 0) {
            throw 'windows_local_build_gpu_smoke_failed'
        }
        $gpuSmoke = 'passed'
    }

    $imageIdProbe = Invoke-PpDocker -Arguments @('image', 'inspect', '--format', '{{.Id}}', $ImageTag)
    if ($imageIdProbe.ExitCode -ne 0) { throw 'windows_local_build_image_id_failed' }
    $imageId = $imageIdProbe.Output.Trim()
    $json = [ordered]@{
        schema_version = 'plastic-promise-local-node-build/v1'
        source_revision = $SourceRevision
        package_version = $packageVersion
        image_tag = $ImageTag
        image_id = $imageId
        container_build_identity = $identityFile
        base_image_digest = $baseImageDigest
        build_policy_digest = $buildPolicyDigest
        recipe_policy_digest = $recipePolicyDigest
        builder = $Builder
        buildkit_image = $BuildkitImage
        docker_hub_mirror = $DockerHubMirror
        build_attempt_limit = $BuildAttempts
        credential_mode = $CredentialMode
        cleanup_report = $cleanupReport
        package_smoke = 'passed'
        gpu_smoke = $gpuSmoke
        completed_at = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Depth 3
    [System.IO.File]::WriteAllText($buildReport, "$json`n", [System.Text.UTF8Encoding]::new($false))
    Write-Output "local inference image build passed: $buildReport"
}

try {
    $wslCompleted = $false
    if ($ExecutionMode -ne 'native-docker') {
        $wslCompleted = Invoke-WslBuild
        if (-not $wslCompleted -and $ExecutionMode -eq 'wsl') {
            throw 'windows_local_build_wsl_path_conversion_failed'
        }
    }
    if (-not $wslCompleted) {
        Write-Output 'WSL build path unavailable; using native Docker Desktop preflight.'
        Invoke-NativeDockerBuild
    }
}
finally {
    Restore-DockerCredentialContext
}
