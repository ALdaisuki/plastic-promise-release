[CmdletBinding()]
param(
    [ValidateSet('desktop-interactive', 'headless-builder')]
    [string]$Mode = 'desktop-interactive',

    [string]$InstallRoot = 'D:\PlasticPromise\release-builder',

    [string]$ReleaseBuilderCommand = 'plastic-promise-release-builder'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$resolvedRoot = [System.IO.Path]::GetFullPath($InstallRoot)
if ($resolvedRoot.StartsWith('C:\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'release_builder_install_root_must_not_use_c_drive'
}
if (-not $resolvedRoot.StartsWith('D:\PlasticPromise\release-builder', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'release_builder_install_root_invalid'
}
if ([string]::IsNullOrWhiteSpace($ReleaseBuilderCommand)) {
    throw 'release_builder_command_required'
}

foreach ($directory in @(
    $resolvedRoot,
    (Join-Path $resolvedRoot 'state'),
    (Join-Path $resolvedRoot 'state\requests'),
    (Join-Path $resolvedRoot 'state\confirmations'),
    (Join-Path $resolvedRoot 'state\receipts'),
    (Join-Path $resolvedRoot 'reports')
)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$configPath = Join-Path $resolvedRoot 'builder-config.json'
$config = [ordered]@{
    schema_version = 'plastic-promise-release-builder-install/v1'
    mode = $Mode
    state_root = (Join-Path $resolvedRoot 'state')
    request_triggered = $true
    persistent_daemon = $false
    release_builder_command = $ReleaseBuilderCommand
    installed_at = [DateTime]::UtcNow.ToString('o')
}
$encoded = ($config | ConvertTo-Json -Depth 3) + [Environment]::NewLine
[System.IO.File]::WriteAllText($configPath, $encoded, [System.Text.UTF8Encoding]::new($false))

$wrapperPath = Join-Path $resolvedRoot 'run-release-builder.ps1'
$wrapper = @'
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$configPath = Join-Path $PSScriptRoot 'builder-config.json'
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($config.persistent_daemon -ne $false -or $config.request_triggered -ne $true) {
    throw 'release_builder_install_contract_invalid'
}
& $config.release_builder_command @Arguments
exit $LASTEXITCODE
'@
[System.IO.File]::WriteAllText($wrapperPath, $wrapper, [System.Text.UTF8Encoding]::new($false))

Write-Output "Release Builder installed: $configPath"
Write-Output "Mode: $Mode"
Write-Output "Request-triggered only; no scheduled task or background daemon was created."
