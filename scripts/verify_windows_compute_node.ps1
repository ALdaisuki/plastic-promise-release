[CmdletBinding()]
param(
    [string]$SourceRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $SourceRoot = Split-Path -Parent $PSScriptRoot
}
$envPath = Join-Path $SourceRoot "deploy\local-inference-node\.env"
$acl = Get-Acl -LiteralPath $envPath
if (-not $acl.AreAccessRulesProtected) { throw "compute_node_env_acl_inheritance_enabled" }
$allowedSids = @(
    [Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
    'S-1-5-18',
    'S-1-5-32-544'
)
foreach ($rule in $acl.Access) {
    if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
    $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($sid -notin $allowedSids) { throw "compute_node_env_acl_principal_invalid" }
}
$values = @{}
Get-Content -LiteralPath $envPath | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#')) {
        $parts = $line -split '=', 2
        if ($parts.Count -eq 2) { $values[$parts[0]] = $parts[1] }
    }
}
$headers = @{ Authorization = $values['PP_LOCAL_NODE_AUTHORIZATION'] }
$health = Invoke-RestMethod -Uri "http://127.0.0.1:19130/health" -Headers $headers -TimeoutSec 10
$identity = Invoke-RestMethod -Uri "http://127.0.0.1:19130/v1/identity" -Headers $headers -TimeoutSec 10
if ($health.status -ne 'ok') { throw "compute_node_health_not_ok" }
if ($identity.node_id -ne $values['PP_LOCAL_NODE_ID']) { throw "compute_node_id_mismatch" }
$capabilities = @($identity.capabilities | ForEach-Object { [string]$_ })
if ($capabilities -notcontains 'embeddings' -or $capabilities -notcontains 'rerank') {
    throw "compute_node_capabilities_invalid"
}
if ($identity.embedding.model -ne $values['PP_LOCAL_NODE_EMBEDDING_MODEL']) {
    throw "compute_node_embedding_model_mismatch"
}
if ($identity.embedding.revision -ne $values['PP_LOCAL_NODE_EMBEDDING_REVISION']) {
    throw "compute_node_embedding_revision_mismatch"
}
$expectedDimension = [int]$values['PP_LOCAL_NODE_EMBEDDING_DIMENSION']
if ([int]$identity.embedding.dimension -ne $expectedDimension) {
    throw "compute_node_embedding_dimension_mismatch"
}
if ($identity.embedding.normalization -ne $values['PP_LOCAL_NODE_EMBEDDING_NORMALIZATION']) {
    throw "compute_node_embedding_normalization_mismatch"
}
if ($values['PP_LOCAL_NODE_EMBEDDING_NORMALIZATION'] -notin @('l2', 'none')) {
    throw "compute_node_embedding_normalization_invalid"
}
if ([string]$values['PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256'] -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "compute_node_expected_embedding_artifact_invalid"
}
if ($identity.embedding.artifact_sha256 -ne $values['PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256']) {
    throw "compute_node_embedding_artifact_mismatch"
}
if ($identity.rerank.model -ne $values['PP_LOCAL_NODE_RERANK_MODEL']) {
    throw "compute_node_rerank_model_mismatch"
}
if ($identity.rerank.revision -ne $values['PP_LOCAL_NODE_RERANK_REVISION']) {
    throw "compute_node_rerank_revision_mismatch"
}
if ([string]$values['PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256'] -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "compute_node_expected_rerank_artifact_invalid"
}
if ($identity.rerank.artifact_sha256 -ne $values['PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256']) {
    throw "compute_node_rerank_artifact_mismatch"
}
$embedding = Invoke-RestMethod -Uri "http://127.0.0.1:19130/v1/embeddings" -Method Post -Headers $headers -ContentType "application/json" -Body (@{
    model = $values['PP_LOCAL_NODE_EMBEDDING_MODEL']
    input = @('Plastic Promise compute smoke')
} | ConvertTo-Json -Compress)
$vector = @($embedding.data[0].embedding | ForEach-Object { [double]$_ })
if ($vector.Count -ne $expectedDimension) { throw "compute_node_embedding_vector_dimension_mismatch" }
$norm = [Math]::Sqrt(($vector | ForEach-Object { $_ * $_ } | Measure-Object -Sum).Sum)
if ([Double]::IsNaN($norm) -or [Double]::IsInfinity($norm)) {
    throw "compute_node_embedding_vector_invalid"
}
if ($values['PP_LOCAL_NODE_EMBEDDING_NORMALIZATION'] -eq 'l2' -and [Math]::Abs($norm - 1.0) -gt 0.0001) {
    throw "compute_node_embedding_l2_mismatch"
}
$rerank = Invoke-RestMethod -Uri "http://127.0.0.1:19130/v1/rerank" -Method Post -Headers $headers -ContentType "application/json" -Body (@{
    query = 'memory governance'
    documents = @('canonical memory governance', 'unrelated cooking recipe')
    top_k = 2
} | ConvertTo-Json -Compress)
$ranked = @($rerank.results | Sort-Object -Property @{
    Expression = {
        if ($null -ne $_.score) { [double]$_.score }
        else { [double]$_.relevance_score }
    }
} -Descending)
$directional = ($ranked.Count -ge 2 -and [int]$ranked[0].index -eq 0)
[pscustomobject]@{
    health_status = $health.status
    node_id = $identity.node_id
    capabilities = $capabilities
    embedding_dimension = $vector.Count
    embedding_l2 = [Math]::Round($norm, 6)
    embedding_artifact_sha256 = $identity.embedding.artifact_sha256
    rerank_directional_probe = $directional
    rerank_model = $values['PP_LOCAL_NODE_RERANK_MODEL']
    rerank_artifact_sha256 = $identity.rerank.artifact_sha256
    rerank_top_index = if ($ranked.Count) { [int]$ranked[0].index } else { -1 }
    rerank_scores = @($ranked | ForEach-Object {
        if ($null -ne $_.score) { [double]$_.score }
        else { [double]$_.relevance_score }
    })
} | ConvertTo-Json -Depth 5
if (-not $directional) { throw "rerank_directional_probe_failed" }
