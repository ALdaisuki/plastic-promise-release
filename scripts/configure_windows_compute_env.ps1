[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceRoot,

    [string]$AuthorizationFile = "",

    [string]$ModelDirectory = "/mnt/d/PlasticPromise/models",

    [string]$NodeId = "inference-node",

    [string]$EmbeddingModel = "Qwen3-Embedding-4B-GGUF",

    [string]$EmbeddingRevision = "f4602530db1d980e16da9d7d3a70294cf5c190be",

    [string]$EmbeddingFile = "embedding/Qwen3-Embedding-4B-Q4_K_M.gguf",

    [ValidateRange(1, 16384)]
    [int]$EmbeddingDimension = 2560,

    [ValidateSet("l2", "none")]
    [string]$EmbeddingNormalization = "l2",

    [string]$EmbeddingArtifactSha256 = "",

    [string]$RerankModel = "Qwen3-Reranker-4B-GGUF",

    [string]$RerankRevision = "1b452c803342e73ac3644551b727dfd51a09fd5b",

    [string]$RerankFile = "rerank/Qwen3-Reranker-4B-Q4_K_M.gguf",

    [string]$RerankArtifactSha256 = "",

    [ValidateSet("off", "cloud", "openai-compatible")]
    [string]$StructuredJsonBackend = "off",

    [string]$StructuredJsonModel = "",

    [string]$StructuredJsonRevision = "",

    [string]$StructuredJsonBaseUrl = "",

    [string]$StructuredJsonPath = "/chat/completions",

    [string]$CloudApiKeyFile = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($NodeId -notmatch '^[a-z][a-z0-9_.:-]{1,127}$') {
    throw "compute_node_id_invalid"
}
$envPath = Join-Path $SourceRoot "deploy\local-inference-node\.env"
$existing = @{}
if (Test-Path -LiteralPath $envPath -PathType Leaf) {
    Get-Content -LiteralPath $envPath | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $parts = $line -split "=", 2
            if ($parts.Count -eq 2) { $existing[$parts[0]] = $parts[1] }
        }
    }
}

$authorization = ""
if ($AuthorizationFile) {
    if (-not (Test-Path -LiteralPath $AuthorizationFile -PathType Leaf)) {
        throw "compute_node_authorization_file_missing"
    }
    $authorization = (Get-Content -Raw -LiteralPath $AuthorizationFile).Trim()
}
elseif ($existing.ContainsKey("PP_LOCAL_NODE_AUTHORIZATION")) {
    $authorization = [string]$existing["PP_LOCAL_NODE_AUTHORIZATION"]
}
if ($authorization -notmatch '^Bearer [A-Za-z0-9._~+/=-]{1,4096}$') {
    throw "compute_node_authorization_invalid"
}

function Get-ExistingValue {
    param([string]$Name, [string]$Default = "")
    if ($existing.ContainsKey($Name) -and -not [string]::IsNullOrWhiteSpace([string]$existing[$Name])) {
        return [string]$existing[$Name]
    }
    return $Default
}

$cloudApiKey = ""
if ($CloudApiKeyFile) {
    if (-not (Test-Path -LiteralPath $CloudApiKeyFile -PathType Leaf)) {
        throw "compute_node_cloud_api_key_file_missing"
    }
    $cloudApiKey = (Get-Content -Raw -LiteralPath $CloudApiKeyFile).Trim()
}
else {
    $cloudApiKey = Get-ExistingValue "PP_LOCAL_NODE_CLOUD_API_KEY"
}

$structuredJsonEnabled = $StructuredJsonBackend -ne "off"
$providerMode = if ($structuredJsonEnabled) { "hybrid" } else { "local" }
if ($structuredJsonEnabled) {
    if (-not $StructuredJsonModel) {
        $StructuredJsonModel = Get-ExistingValue "PP_LOCAL_NODE_STRUCTURED_JSON_MODEL"
    }
    if (-not $StructuredJsonRevision) {
        $StructuredJsonRevision = Get-ExistingValue "PP_LOCAL_NODE_STRUCTURED_JSON_REVISION"
    }
    if (-not $StructuredJsonBaseUrl) {
        $StructuredJsonBaseUrl = Get-ExistingValue "PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_BASE_URL"
    }
    if (-not $StructuredJsonModel) { throw "compute_node_structured_json_model_required" }
    if ($StructuredJsonRevision -notmatch '^(?:[0-9a-f]{40}|sha256:[0-9a-f]{64})$') {
        throw "compute_node_structured_json_revision_must_be_pinned"
    }
    $structuredUri = $null
    if (-not [Uri]::TryCreate($StructuredJsonBaseUrl, [UriKind]::Absolute, [ref]$structuredUri) -or
        $structuredUri.Scheme -ne "https" -or $structuredUri.UserInfo) {
        throw "compute_node_structured_json_base_url_invalid"
    }
    if ($StructuredJsonPath -notmatch '^/[A-Za-z0-9._~!$&''()*+,;=:@%/-]{1,1023}$') {
        throw "compute_node_structured_json_path_invalid"
    }
    if ($cloudApiKey -notmatch '^[A-Za-z0-9._~+/=:@%-]{8,4096}$') {
        throw "compute_node_cloud_api_key_invalid"
    }
}

if (-not $EmbeddingArtifactSha256) {
    $EmbeddingArtifactSha256 = Get-ExistingValue "PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256"
}
if (-not $RerankArtifactSha256) {
    $RerankArtifactSha256 = Get-ExistingValue "PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256"
}
if ($EmbeddingArtifactSha256 -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "compute_node_embedding_artifact_sha256_invalid"
}
if ($RerankArtifactSha256 -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "compute_node_rerank_artifact_sha256_invalid"
}

$sourceRevision = Get-ExistingValue "PP_BUILD_SOURCE_REVISION"
if (-not $sourceRevision) {
    $sourceRevision = (& git -C $SourceRoot rev-parse HEAD 2>$null).Trim()
}
if ($sourceRevision -notmatch '^[0-9a-fA-F]{40}$') {
    throw "compute_node_source_revision_invalid"
}
$packageVersion = Get-ExistingValue "PP_BUILD_PACKAGE_VERSION"
if (-not $packageVersion) {
    $match = Select-String -LiteralPath (Join-Path $SourceRoot "pyproject.toml") -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($match) { $packageVersion = $match.Matches[0].Groups[1].Value }
}
$cudaBase = Get-ExistingValue "PP_COMPUTE_CUDA_BASE_IMAGE"
$cudaDigest = Get-ExistingValue "PP_COMPUTE_CUDA_BASE_IMAGE_DIGEST"
$buildPolicyDigest = Get-ExistingValue "PP_BUILD_POLICY_DIGEST"
$recipePolicyDigest = Get-ExistingValue "PP_RECIPE_POLICY_DIGEST"
if (-not $cudaBase -or -not $cudaDigest -or -not $buildPolicyDigest -or -not $recipePolicyDigest) {
    throw "compute_node_build_identity_missing: run the immutable compute image build first"
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $envPath) | Out-Null
$lines = @(
    "PP_COMPUTE_CUDA_BASE_IMAGE=$cudaBase",
    "PP_COMPUTE_CUDA_BASE_IMAGE_DIGEST=$cudaDigest",
    "PP_BUILD_SOURCE_REVISION=$sourceRevision",
    "PP_BUILD_PACKAGE_VERSION=$packageVersion",
    "PP_BUILD_POLICY_DIGEST=$buildPolicyDigest",
    "PP_RECIPE_POLICY_DIGEST=$recipePolicyDigest",
    "PP_LOCAL_NODE_AUTHORIZATION=$authorization",
    "PP_LOCAL_NODE_ID=$NodeId",
    "PP_LOCAL_NODE_MODEL_DIRECTORY=$ModelDirectory",
    "PP_LOCAL_NODE_PROVIDER_MODE=$providerMode",
    "PP_LOCAL_NODE_EMBEDDING_BACKEND=llama.cpp",
    "PP_LOCAL_NODE_EMBEDDING_MODEL=$EmbeddingModel",
    "PP_LOCAL_NODE_EMBEDDING_REVISION=$EmbeddingRevision",
    "PP_LOCAL_NODE_EMBEDDING_DIMENSION=$EmbeddingDimension",
    "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION=$EmbeddingNormalization",
    "PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256=$EmbeddingArtifactSha256",
    "PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE=/models/$EmbeddingFile",
    "PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL=http://127.0.0.1:19131",
    "PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH=/v1/embeddings",
    "PP_LOCAL_NODE_RERANK_BACKEND=llama.cpp",
    "PP_LOCAL_NODE_RERANK_MODEL=$RerankModel",
    "PP_LOCAL_NODE_RERANK_REVISION=$RerankRevision",
    "PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256=$RerankArtifactSha256",
    "PP_LOCAL_NODE_RERANK_MODEL_REFERENCE=/models/$RerankFile",
    "PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL=http://127.0.0.1:19132",
    "PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH=/rerank",
    "PP_LOCAL_NODE_STRUCTURED_JSON_BACKEND=$StructuredJsonBackend",
    "PP_LOCAL_NODE_BIND_HOST=127.0.0.1",
    "PP_LOCAL_NODE_PORT=19130",
    "PP_LOCAL_NODE_MAX_CONCURRENCY=1"
)

if ($structuredJsonEnabled) {
    $lines += @(
        "PP_LOCAL_NODE_STRUCTURED_JSON_MODEL=$StructuredJsonModel",
        "PP_LOCAL_NODE_STRUCTURED_JSON_REVISION=$StructuredJsonRevision",
        "PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_BASE_URL=$StructuredJsonBaseUrl",
        "PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_PATH=$StructuredJsonPath",
        "PP_LOCAL_NODE_CLOUD_API_KEY=$cloudApiKey"
    )
}

[IO.File]::WriteAllLines($envPath, $lines, (New-Object Text.UTF8Encoding($false)))
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $envPath /inheritance:r /grant:r `
    "*$($currentSid):(F)" `
    "*S-1-5-18:(F)" `
    "*S-1-5-32-544:(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "compute_node_env_acl_update_failed" }
if ($AuthorizationFile) {
    Remove-Item -LiteralPath $AuthorizationFile -Force
}
if ($CloudApiKeyFile) {
    Remove-Item -LiteralPath $CloudApiKeyFile -Force
}
Write-Output "compute_env_written=$envPath"
