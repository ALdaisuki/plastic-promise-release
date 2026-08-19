[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$pythonBin = if ([string]::IsNullOrWhiteSpace($env:PP_PYTHON)) {
    "python"
} else {
    $env:PP_PYTHON
}

function Resolve-WslDistribution {
    param(
        [Parameter(Mandatory = $true)]
        [string]$WslExecutable
    )

    if (![string]::IsNullOrWhiteSpace($env:PP_WSL_DISTRIBUTION)) {
        $requestedDistribution = $env:PP_WSL_DISTRIBUTION.Trim()
        if ($requestedDistribution -like "docker-*") {
            throw "wsl_distribution_not_allowed"
        }
        return $requestedDistribution
    }

    $candidates = @(
        & $WslExecutable --list --quiet 2>$null |
            ForEach-Object { $_.Trim() } |
            Where-Object {
                ![string]::IsNullOrWhiteSpace($_) -and
                $_ -notlike "docker-*"
            }
    )
    if ($LASTEXITCODE -ne 0 -or $candidates.Count -ne 1) {
        throw "wsl_distribution_required"
    }
    return $candidates[0]
}

function Resolve-WslRepositoryRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$WslExecutable,
        [Parameter(Mandatory = $true)]
        [string]$Distribution
    )

    $repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $wslRoot = (& $WslExecutable --distribution $Distribution --exec wslpath -a $repositoryRoot 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($wslRoot)) {
        throw "wsl_repository_mapping_failed"
    }
    return $wslRoot
}

function Get-ExpectedPackageVersion {
    $packageInit = Join-Path (Join-Path $PSScriptRoot "..") "plastic_promise\__init__.py"
    if (!(Test-Path -LiteralPath $packageInit)) {
        throw "wsl_package_version_source_missing"
    }
    $match = Select-String -LiteralPath $packageInit -Pattern '^__version__\s*=\s*"([^"]+)"\s*$' |
        Select-Object -First 1
    if ($null -eq $match) {
        throw "wsl_package_version_source_invalid"
    }
    return $match.Matches[0].Groups[1].Value
}

$useWsl = $env:PP_DEPLOY_TARGET -eq "wsl"
if (!$useWsl) {
    $pythonCommand = Get-Command $pythonBin -ErrorAction SilentlyContinue
    $useWsl = $null -eq $pythonCommand
}

if ($useWsl) {
    $wslExe = Get-Command "wsl.exe" -ErrorAction SilentlyContinue
    if ($null -eq $wslExe) {
        Write-Error "WSL2 deployment target requested, but wsl.exe is unavailable."
        exit 127
    }
    $wslPython = if ([string]::IsNullOrWhiteSpace($env:PP_WSL_PYTHON)) {
        "python3"
    } else {
        $env:PP_WSL_PYTHON
    }
    try {
        $wslDistribution = Resolve-WslDistribution -WslExecutable $wslExe.Source
        $wslRoot = Resolve-WslRepositoryRoot -WslExecutable $wslExe.Source -Distribution $wslDistribution
        $expectedVersion = Get-ExpectedPackageVersion
        $versionCheck = @"
import plastic_promise
expected = '$expectedVersion'
actual = getattr(plastic_promise, '__version__', '')
if actual != expected:
    raise SystemExit('plastic_promise_version_mismatch')
"@
        & $wslExe.Source --distribution $wslDistribution --cd $wslRoot --exec $wslPython -c $versionCheck
        if ($LASTEXITCODE -ne 0) {
            throw "wsl_package_identity_check_failed"
        }
    } catch {
        Write-Error $_
        exit 126
    }
    & $wslExe.Source --distribution $wslDistribution --cd $wslRoot --exec $wslPython -m plastic_promise.deployment @Arguments
    exit $LASTEXITCODE
}

& $pythonBin -m plastic_promise.deployment @Arguments
exit $LASTEXITCODE
