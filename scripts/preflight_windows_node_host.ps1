#Requires -Version 5.1
<#
.SYNOPSIS
    Idempotent Windows/WSL2 host preflight for the Plastic Promise local
    inference node. Detects the Docker runtime (Docker Desktop vs WSL2 native
    daemon), bridges the WSL daemon to a Windows docker context when Desktop
    is unavailable, checks system-disk headroom and VHDX placement, merges
    .wslconfig defaults, verifies systemd + docker autostart, and detects the
    local proxy that WSL builds need.

.DESCRIPTION
    This script is the first stage of the fully automated Windows compute-node
    setup. It never writes credentials, model weights, or private endpoints.
    Everything it writes is an idempotent host-level configuration:

      * Docker runtime detection and WSL-native bridge (socat + docker context)
      * system-disk headroom check and optional VHDX migration
      * .wslconfig merge (memory/processors/swap/vmIdleTimeout/systemd)
      * systemd + docker.service autostart verification
      * proxy detection and WSL /etc/profile.d export

    Exit code 0 means the host is ready for the next setup stage. The report
    is printed as JSON and optionally written to -OutputPath.

.PARAMETER ProfilePath
    Operator profile (KEY=VALUE lines). Optional; all values fall back to
    parameters or auto-detection.

.PARAMETER WslDistro
    WSL distribution hosting the native Docker daemon. Default Ubuntu-22.04.

.PARAMETER MinFreeSystemGb
    Minimum free system-disk headroom in GB before the VHDX placement warning
    becomes an error. Default 20.

.PARAMETER MigrateVhdxTo
    When set (e.g. D:\WSL), automatically migrates the WSL VHDX off the system
    disk. Requires the distro to be stopped; the script terminates it first.
    This is an explicit, user-authorized mutation.

.PARAMETER ProxyUrl
    Explicit proxy URL (e.g. http://127.0.0.1:7897). When omitted the script
    probes common local proxy ports only if direct connectivity fails.

.PARAMETER EnableDockerBridge
    Opt in to the optional WSL-native docker bridge / Windows context. The
    normal build path invokes Docker through WSL directly and does not need
    this host-global convenience context.

.PARAMETER OutputPath
    Optional JSON report destination.
#>
[CmdletBinding()]
param(
    [string]$ProfilePath = '',
    [string]$WslDistro = 'Ubuntu-22.04',
    [ValidateRange(5, 200)]
    [int]$MinFreeSystemGb = 20,
    [string]$MigrateVhdxTo = '',
    [string]$ProxyUrl = '',
    [switch]$EnableDockerBridge,
    [string]$OutputPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-PpLog {
    param([string]$Message)
    Write-Output ("[preflight-windows-node-host] " + $Message)
}

function Read-PpProfile {
    param([string]$Path)
    $map = @{}
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $map }
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

function Invoke-PpWsl {
    param([string]$Command)
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $output = & wsl.exe --distribution $WslDistro -e bash -lc $Command 2>&1
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

function Invoke-PpWslExec {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [switch]$AsRoot
    )
    $wslArguments = @('--distribution', $WslDistro)
    if ($AsRoot) { $wslArguments += @('--user', 'root') }
    $wslArguments += @('--exec') + $Arguments
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $output = & wsl.exe @wslArguments 2>&1
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

function Invoke-PpDockerQuiet {
    param([string[]]$Arguments)
    # PS 5.1 turns native stderr into terminating errors under
    # $ErrorActionPreference=Stop; probe docker without failing the preflight.
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $output = & docker.exe @Arguments 2>$null
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
    }
    catch {
        return [pscustomobject]@{ ExitCode = -1; Output = '' }
    }
    finally {
        $ErrorActionPreference = $oldEap
    }
}

function Test-PpDesktopDocker {
    $cmd = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { return $false }
    $probe = Invoke-PpDockerQuiet -Arguments @('--context', 'desktop-linux', 'version', '--format', '{{.Server.Version}}')
    return ($probe.ExitCode -eq 0 -and $probe.Output -match '^[0-9]')
}

function Test-PpWslDocker {
    $r = Invoke-PpWsl -Command 'docker version --format "{{.Server.Version}}"'
    return ($r.ExitCode -eq 0 -and $r.Output -match '^[0-9]')
}

function Get-PpActiveDockerContext {
    $list = Invoke-PpDockerQuiet -Arguments @('context', 'ls', '--format', "{{.Name}}`t{{if .Current}}*{{end}}")
    if ($list.ExitCode -ne 0) { return '' }
    foreach ($line in @($list.Output)) {
        if ($line -match '^\S+\t\*$') {
            return ($line -split "`t")[0]
        }
    }
    return ''
}

function Get-PpWslDockerVersion {
    $r = Invoke-PpWsl -Command 'docker version --format "{{.Server.Version}}"'
    if ($r.ExitCode -eq 0) { return $r.Output }
    return ''
}

function ConvertTo-PpWslPath {
    param([string]$WindowsPath)
    $drive = $WindowsPath.Substring(0, 1).ToLowerInvariant()
    $rest = $WindowsPath.Substring(3).Replace('\', '/')
    return "/mnt/$drive/$rest"
}

function Write-PpTempFile {
    param([string]$Content, [string]$Name)
    $tempDir = [System.IO.Path]::GetTempPath()
    $hostPath = Join-Path $tempDir $Name
    [System.IO.File]::WriteAllText($hostPath, $Content, [System.Text.UTF8Encoding]::new($false))
    return $hostPath
}

function Ensure-PpSocatBridge {
    param($Profile)
    if (-not $EnableDockerBridge) {
        Write-PpLog 'optional docker bridge not enabled'
        return [pscustomobject]@{ Context = ''; Connected = $false; Reason = 'skipped' }
    }
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ Context = ''; Connected = $false; Reason = 'wsl-not-available' }
    }
    $proxy = Get-PpValue $Profile 'PP_PROXY_URL' $ProxyUrl
    # install socat when missing (apt goes through the proxy when detected)
    $socatCheck = Invoke-PpWsl -Command 'command -v socat >/dev/null 2>&1 && echo present || echo missing'
    if ($socatCheck.Output -match 'missing') {
        Write-PpLog 'installing socat inside WSL (required for the docker TCP bridge)'
        $proxyArguments = @()
        if ($proxy) {
            $proxyArguments = @("http_proxy=$proxy", "https_proxy=$proxy", "all_proxy=$proxy")
        }
        $aptUpdate = Invoke-PpWslExec -AsRoot -Arguments (@('env', 'DEBIAN_FRONTEND=noninteractive') + $proxyArguments + @('apt-get', 'update', '-qq'))
        $aptInstall = if ($aptUpdate.ExitCode -eq 0) {
            Invoke-PpWslExec -AsRoot -Arguments (@('env', 'DEBIAN_FRONTEND=noninteractive') + $proxyArguments + @('apt-get', 'install', '-y', '--no-install-recommends', 'socat'))
        }
        else {
            [pscustomobject]@{ ExitCode = $aptUpdate.ExitCode; Output = $aptUpdate.Output }
        }
        if ($aptInstall.ExitCode -ne 0) {
            Write-PpLog "socat install failed: $($aptInstall.Output)"
            return [pscustomobject]@{ Context = ''; Connected = $false; Reason = 'socat-install-failed' }
        }
    }
    $unit = @'
[Unit]
Description=Plastic Promise Docker TCP bridge (loopback only)
After=docker.service
Requires=docker.service

[Service]
ExecStart=/usr/bin/socat TCP-LISTEN:2375,fork,bind=127.0.0.1 UNIX-CONNECT:/var/run/docker.sock
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
'@
    $hostUnit = Write-PpTempFile -Content $unit -Name 'pp-docker-tcp-bridge.service'
    $wslUnit = ConvertTo-PpWslPath -WindowsPath $hostUnit
    $unitInstall = Invoke-PpWslExec -AsRoot -Arguments @('install', '-m', '644', $wslUnit, '/etc/systemd/system/pp-docker-tcp-bridge.service')
    $daemonReload = Invoke-PpWslExec -AsRoot -Arguments @('systemctl', 'daemon-reload')
    $install = Invoke-PpWslExec -AsRoot -Arguments @('systemctl', 'enable', '--now', 'pp-docker-tcp-bridge.service')
    if ($unitInstall.ExitCode -ne 0 -or $daemonReload.ExitCode -ne 0 -or $install.ExitCode -ne 0) {
        Write-PpLog "bridge service install failed: $($unitInstall.Output) $($daemonReload.Output) $($install.Output)"
        return [pscustomobject]@{ Context = ''; Connected = $false; Reason = 'bridge-service-failed' }
    }
    # create/refresh the Windows docker context (mirrored networking shares loopback)
    $contextName = 'pp-wsl'
    $contextList = Invoke-PpDockerQuiet -Arguments @('context', 'ls', '--format', '{{.Name}}')
    if ($contextList.ExitCode -eq 0 -and @($contextList.Output) -contains $contextName) {
        $null = Invoke-PpDockerQuiet -Arguments @('context', 'update', $contextName, '--docker', 'host=tcp://127.0.0.1:2375')
    }
    else {
        $null = Invoke-PpDockerQuiet -Arguments @('context', 'create', $contextName, '--docker', 'host=tcp://127.0.0.1:2375')
    }
    $null = Invoke-PpDockerQuiet -Arguments @('context', 'use', $contextName)
    $verify = Invoke-PpDockerQuiet -Arguments @('--context', $contextName, 'version', '--format', '{{.Server.Version}}')
    $connected = ($verify.ExitCode -eq 0 -and $verify.Output -match '^[0-9]')
    return [pscustomobject]@{ Context = $contextName; Connected = $connected; Reason = if ($connected) { 'ok' } else { 'context-not-connected' } }
}

function Test-PpSystemDiskHeadroom {
    $systemDriveName = if ($env:SystemDrive) { $env:SystemDrive.TrimEnd(':') } else { 'C' }
    $drive = Get-PSDrive -Name $systemDriveName -ErrorAction SilentlyContinue
    if (-not $drive) {
        return [pscustomobject]@{
            FreeGb = -1
            Ok = $true
            VhdxOnSystemDisk = $false
            VhdxPath = ''
            Message = "$systemDriveName`: not present"
        }
    }
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    $systemPrefix = "$systemDriveName`:"
    $vhdxOnSystem = $false
    $vhdxPath = ''

    # Modern and imported distributions expose their base path in the per-user
    # Lxss registry. Prefer that over an expensive recursive package scan.
    $lxssRoot = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
    if (Test-Path -LiteralPath $lxssRoot) {
        foreach ($key in @(Get-ChildItem -LiteralPath $lxssRoot -ErrorAction SilentlyContinue)) {
            try {
                $item = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction Stop
                if ([string]$item.DistributionName -eq $WslDistro -and $item.BasePath) {
                    $basePath = [Environment]::ExpandEnvironmentVariables([string]$item.BasePath)
                    foreach ($candidate in @(
                        (Join-Path $basePath 'ext4.vhdx'),
                        (Join-Path $basePath 'LocalState\ext4.vhdx')
                    )) {
                        if (Test-Path -LiteralPath $candidate) {
                            $vhdxPath = [System.IO.Path]::GetFullPath($candidate)
                            break
                        }
                    }
                }
            }
            catch { }
            if ($vhdxPath) { break }
        }
    }
    if (-not $vhdxPath) {
        $packageRoot = Join-Path $env:LOCALAPPDATA 'Packages'
        if (Test-Path -LiteralPath $packageRoot) {
            $candidate = Get-ChildItem -LiteralPath $packageRoot -Recurse -Filter 'ext4.vhdx' -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($candidate) { $vhdxPath = $candidate.FullName }
        }
    }
    if ($vhdxPath) {
        $vhdxOnSystem = $vhdxPath.StartsWith("$systemPrefix\", [System.StringComparison]::OrdinalIgnoreCase)
    }
    $ok = ($freeGb -ge $MinFreeSystemGb)
    return [pscustomobject]@{
        FreeGb = $freeGb
        Ok = $ok
        VhdxOnSystemDisk = $vhdxOnSystem
        VhdxPath = $vhdxPath
        Message = if ($ok) { "system disk headroom ok ($freeGb GB free)" } else { "system disk headroom low: $freeGb GB < $MinFreeSystemGb GB" }
    }
}

function Invoke-PpVhdxMigration {
    param([string]$Target)
    if (-not $Target) { return $null }
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        Write-PpLog 'wsl.exe not available; VHDX migration skipped'
        return [pscustomobject]@{ Migrated = $false; Reason = 'wsl-not-available' }
    }
    $targetPath = [System.IO.Path]::GetFullPath($Target)
    $systemDrive = if ($env:SystemDrive) { $env:SystemDrive.TrimEnd(':') } else { 'C' }
    if ($targetPath.StartsWith("$systemDrive`:\", [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-PpLog "VHDX migration target must be outside the system drive: $targetPath"
        return [pscustomobject]@{ Migrated = $false; Reason = 'target-on-system-disk' }
    }
    if (-not (Test-Path -LiteralPath $targetPath)) {
        New-Item -ItemType Directory -Force -Path $targetPath | Out-Null
    }
    Write-PpLog "terminating $WslDistro before VHDX migration"
    $null = & wsl.exe --terminate $WslDistro 2>$null
    $null = & wsl.exe --manage $WslDistro --move $targetPath 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-PpLog "VHDX migration failed (exit $code); run manually: wsl --manage $WslDistro --move `"$targetPath`""
        return [pscustomobject]@{ Migrated = $false; Reason = "move-exit-$code" }
    }
    Write-PpLog "VHDX migrated to $targetPath"
    return [pscustomobject]@{ Migrated = $true; Reason = 'ok' }
}

function Ensure-PpWslConfig {
    param($Profile)
    $configPath = Join-Path $env:USERPROFILE '.wslconfig'
    $logicalProcessors = [Math]::Max(1, [Environment]::ProcessorCount)
    $recommendedProcessors = [Math]::Max(2, [Math]::Min(8, $logicalProcessors - 1))
    $totalMemoryGb = 16
    try {
        $totalMemoryGb = [Math]::Max(8, [Math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB))
    }
    catch { }
    $recommendedMemoryGb = [Math]::Max(4, [Math]::Min(16, [Math]::Floor($totalMemoryGb * 0.5)))
    $recommendedSwapGb = [Math]::Max(2, [Math]::Min(8, [Math]::Ceiling($recommendedMemoryGb / 4)))

    $defaults = [ordered]@{
        'wsl2' = [ordered]@{
            'memory' = Get-PpValue $Profile 'PP_WSL_MEMORY' "$recommendedMemoryGb`GB"
            'processors' = Get-PpValue $Profile 'PP_WSL_PROCESSORS' "$recommendedProcessors"
            'swap' = Get-PpValue $Profile 'PP_WSL_SWAP' "$recommendedSwapGb`GB"
            'vmIdleTimeout' = Get-PpValue $Profile 'PP_WSL_VM_IDLE_TIMEOUT' '6000000'
        }
    }
    $sourceLines = if (Test-Path -LiteralPath $configPath) { @(Get-Content -LiteralPath $configPath) } else { @() }
    $outside = New-Object System.Collections.Generic.List[string]
    $wsl2Extras = New-Object System.Collections.Generic.List[string]
    $marker = '__PLASTIC_PROMISE_WSL2_SECTION__'
    $currentSection = ''
    $wsl2Seen = $false
    $legacyBootNoticeWritten = $false
    $managedCounts = @{}
    foreach ($key in $defaults['wsl2'].Keys) { $managedCounts[$key.ToLowerInvariant()] = 0 }

    foreach ($line in $sourceLines) {
        if ($line -match '^\s*\[([^\]]+)\]\s*$') {
            $nextSection = $matches[1].ToLowerInvariant()
            if ($nextSection -eq 'boot') {
                $currentSection = 'legacy-boot'
                if (-not $legacyBootNoticeWritten) {
                    $outside.Add('# Plastic Promise moved the legacy [boot] block to /etc/wsl.conf.')
                    $legacyBootNoticeWritten = $true
                }
                continue
            }
            if ($nextSection -eq 'wsl2') {
                $currentSection = 'wsl2'
                if (-not $wsl2Seen) {
                    $outside.Add($marker)
                    $wsl2Seen = $true
                }
                continue
            }
            $currentSection = $nextSection
            $outside.Add($line)
            continue
        }
        if ($currentSection -eq 'legacy-boot') {
            if (-not [string]::IsNullOrWhiteSpace($line)) {
                $outside.Add('# legacy .wslconfig [boot]: ' + $line)
            }
            continue
        }
        if ($currentSection -eq 'wsl2') {
            if ($line -match '^\s*([^#=\s]+)\s*=') {
                $matchedKey = $matches[1].ToLowerInvariant()
                if ($managedCounts.ContainsKey($matchedKey)) {
                    $managedCounts[$matchedKey] = [int]$managedCounts[$matchedKey] + 1
                    continue
                }
            }
            $wsl2Extras.Add($line)
            continue
        }
        $outside.Add($line)
    }

    if (-not $wsl2Seen) {
        if ($outside.Count -gt 0 -and $outside[$outside.Count - 1] -ne '') { $outside.Add('') }
        $outside.Add($marker)
    }

    $wsl2Block = New-Object System.Collections.Generic.List[string]
    $wsl2Block.Add('[wsl2]')
    foreach ($key in $defaults['wsl2'].Keys) {
        $wsl2Block.Add("$key=$($defaults['wsl2'][$key])")
    }
    foreach ($line in $wsl2Extras) { $wsl2Block.Add($line) }

    $output = New-Object System.Collections.Generic.List[string]
    foreach ($line in $outside) {
        if ($line -eq $marker) {
            foreach ($wslLine in $wsl2Block) { $output.Add($wslLine) }
        }
        else {
            $output.Add($line)
        }
    }

    $content = ($output -join "`r`n").TrimEnd() + "`r`n"
    $originalContent = if ($sourceLines.Count -gt 0) {
        ($sourceLines -join "`r`n").TrimEnd() + "`r`n"
    }
    else { '' }
    $changed = ($content -ne $originalContent)
    if ($changed -or -not (Test-Path -LiteralPath $configPath)) {
        [System.IO.File]::WriteAllText($configPath, $content, [System.Text.UTF8Encoding]::new($false))
    }

    return [pscustomobject]@{
        Changed = $changed
        Path = $configPath
        NeedsRestart = $changed
        Memory = $defaults['wsl2']['memory']
        Processors = $defaults['wsl2']['processors']
        Swap = $defaults['wsl2']['swap']
    }
}

function Ensure-PpWslSystemdConfig {
    $read = Invoke-PpWsl -Command 'if [ -f /etc/wsl.conf ]; then cat /etc/wsl.conf; fi'
    $lines = if ($read.ExitCode -eq 0 -and $read.Output) { @($read.Output -split "`r?`n") } else { @() }
    $output = New-Object System.Collections.Generic.List[string]
    $currentSection = ''
    $bootFound = $false
    $systemdFound = $false
    $changed = $false
    foreach ($line in $lines) {
        if ($line -match '^\s*\[([^\]]+)\]\s*$') {
            if ($currentSection -eq 'boot' -and -not $systemdFound) {
                $output.Add('systemd=true')
                $systemdFound = $true
                $changed = $true
            }
            $currentSection = $matches[1].ToLowerInvariant()
            if ($currentSection -eq 'boot') { $bootFound = $true }
            $output.Add($line)
            continue
        }
        if ($currentSection -eq 'boot' -and $line -match '^\s*systemd\s*=') {
            $systemdFound = $true
            if ($line.Trim().ToLowerInvariant() -ne 'systemd=true') {
                $output.Add('systemd=true')
                $changed = $true
            }
            else {
                $output.Add($line)
            }
            continue
        }
        $output.Add($line)
    }
    if ($currentSection -eq 'boot' -and -not $systemdFound) {
        $output.Add('systemd=true')
        $systemdFound = $true
        $changed = $true
    }
    if (-not $bootFound) {
        if ($output.Count -gt 0 -and $output[$output.Count - 1] -ne '') { $output.Add('') }
        $output.Add('[boot]')
        $output.Add('systemd=true')
        $changed = $true
    }
    if ($changed -or $lines.Count -eq 0) {
        $content = ($output -join "`n").TrimEnd() + "`n"
        $hostFile = Write-PpTempFile -Content $content -Name 'pp-wsl.conf'
        $wslFile = ConvertTo-PpWslPath -WindowsPath $hostFile
        $install = Invoke-PpWslExec -AsRoot -Arguments @('install', '-m', '644', $wslFile, '/etc/wsl.conf')
        if ($install.ExitCode -ne 0) {
            return [pscustomobject]@{ Changed = $false; Installed = $false; Reason = $install.Output }
        }
    }
    return [pscustomobject]@{ Changed = $changed; Installed = $true; Reason = 'ok' }
}

function Test-PpSystemdDocker {
    $running = Invoke-PpWsl -Command 'systemctl is-system-running 2>/dev/null || true'
    $dockerEnabled = Invoke-PpWsl -Command 'systemctl is-enabled docker 2>/dev/null || echo not-enabled'
    $enableResult = $null
    if ($dockerEnabled.Output -ne 'enabled') {
        Write-PpLog 'docker.service not enabled; enabling autostart'
        $enableResult = Invoke-PpWslExec -AsRoot -Arguments @('systemctl', 'enable', '--now', 'docker')
    }
    $active = Invoke-PpWsl -Command 'systemctl is-active docker 2>/dev/null || echo inactive'
    return [pscustomobject]@{
        SystemdState = $running.Output
        DockerEnabled = ($dockerEnabled.Output -eq 'enabled' -or ($null -ne $enableResult -and $enableResult.ExitCode -eq 0))
        DockerActive = ($active.Output -eq 'active')
        NeedsWslRestart = ($running.Output -notmatch 'running')
    }
}

function Test-PpProxy {
    param([switch]$UseWsl)
    $directOk = $false
    if ($UseWsl) {
        $probe = Invoke-PpWslExec -Arguments @('curl', '-fsS', '--max-time', '8', '-o', '/dev/null', 'https://huggingface.co')
        $directOk = ($probe.ExitCode -eq 0)
    }
    else {
        try {
            $code = & curl.exe -s -m 8 -o NUL -w '%{http_code}' https://huggingface.co 2>$null
            $directOk = ($LASTEXITCODE -eq 0 -and $code -match '^[23]')
        }
        catch { $directOk = $false }
    }
    if ($directOk) { return [pscustomobject]@{ DirectOk = $true; ProxyUrl = '' } }
    $candidates = @('http://127.0.0.1:7897', 'http://127.0.0.1:10809', 'http://127.0.0.1:1080')
    if ($ProxyUrl) { $candidates = @($ProxyUrl) + $candidates }
    if ($UseWsl) {
        $gatewayProbe = Invoke-PpWsl -Command "ip route show default | awk '{print `$3; exit}'"
        $gateway = if ($gatewayProbe.ExitCode -eq 0) { $gatewayProbe.Output.Trim() } else { '' }
        $wslCandidates = New-Object System.Collections.Generic.List[string]
        foreach ($candidate in $candidates) {
            if (-not $wslCandidates.Contains($candidate)) { $wslCandidates.Add($candidate) }
            try {
                $uri = [System.Uri]$candidate
                if ($gateway -and $uri.IsLoopback) {
                    $builder = New-Object System.UriBuilder($uri)
                    $builder.Host = $gateway
                    $gatewayCandidate = $builder.Uri.AbsoluteUri.TrimEnd('/')
                    if (-not $wslCandidates.Contains($gatewayCandidate)) { $wslCandidates.Add($gatewayCandidate) }
                }
            }
            catch { }
        }
        foreach ($candidate in $wslCandidates) {
            $proxyProbe = Invoke-PpWslExec -Arguments @('curl', '-fsS', '--max-time', '8', '--proxy', $candidate, '-o', '/dev/null', 'https://huggingface.co')
            if ($proxyProbe.ExitCode -eq 0) {
                return [pscustomobject]@{ DirectOk = $false; ProxyUrl = $candidate }
            }
        }
        return [pscustomobject]@{ DirectOk = $false; ProxyUrl = '' }
    }
    foreach ($candidate in $candidates) {
        try {
            $code = & curl.exe -s -m 8 -x $candidate -o NUL -w '%{http_code}' https://huggingface.co 2>$null
            if ($LASTEXITCODE -eq 0 -and $code -match '^[23]') {
                return [pscustomobject]@{ DirectOk = $false; ProxyUrl = $candidate }
            }
        }
        catch { }
    }
    return [pscustomobject]@{ DirectOk = $false; ProxyUrl = '' }
}

function Write-PpWslProxyProfile {
    param([string]$ProxyUrl)
    if (-not $ProxyUrl) { return $false }
    $singleQuote = [string][char]39
    $doubleQuote = [string][char]34
    $singleQuoteEscape = $singleQuote + $doubleQuote + $singleQuote + $doubleQuote + $singleQuote
    $quotedProxy = $singleQuote + $ProxyUrl.Replace($singleQuote, $singleQuoteEscape) + $singleQuote
    $content = @(
        "export http_proxy=$quotedProxy",
        "export https_proxy=$quotedProxy",
        "export all_proxy=$quotedProxy",
        'export no_proxy=localhost,127.0.0.1,192.168.0.0/16'
    ) -join "`n"
    $existing = Invoke-PpWsl -Command 'if [ -f /etc/profile.d/pp-proxy.sh ]; then cat /etc/profile.d/pp-proxy.sh; fi'
    if ($existing.ExitCode -eq 0 -and $existing.Output.Trim() -eq $content.Trim()) {
        return $true
    }
    $hostFile = Write-PpTempFile -Content ($content + "`n") -Name 'pp-proxy.sh'
    $wslFile = ConvertTo-PpWslPath -WindowsPath $hostFile
    $r = Invoke-PpWslExec -AsRoot -Arguments @('install', '-m', '644', $wslFile, '/etc/profile.d/pp-proxy.sh')
    return ($r.ExitCode -eq 0)
}

function Write-PpDockerProxyDropIn {
    param([string]$ProxyUrl)
    if (-not $ProxyUrl) { return $false }
    $escaped = $ProxyUrl.Replace('\', '\\').Replace('"', '\"').Replace('%', '%%')
    $content = @(
        '[Service]',
        "Environment=`"HTTP_PROXY=$escaped`"",
        "Environment=`"HTTPS_PROXY=$escaped`"",
        'Environment="NO_PROXY=localhost,127.0.0.1,::1"'
    ) -join "`n"
    $existing = Invoke-PpWsl -Command 'if [ -f /etc/systemd/system/docker.service.d/pp-proxy.conf ]; then cat /etc/systemd/system/docker.service.d/pp-proxy.conf; fi'
    if ($existing.ExitCode -eq 0 -and $existing.Output.Trim() -eq $content.Trim()) {
        return $true
    }
    $hostFile = Write-PpTempFile -Content ($content + "`n") -Name 'pp-docker-proxy.conf'
    $wslFile = ConvertTo-PpWslPath -WindowsPath $hostFile
    $mkdir = Invoke-PpWslExec -AsRoot -Arguments @('mkdir', '-p', '/etc/systemd/system/docker.service.d')
    $install = Invoke-PpWslExec -AsRoot -Arguments @('install', '-m', '644', $wslFile, '/etc/systemd/system/docker.service.d/pp-proxy.conf')
    $reload = Invoke-PpWslExec -AsRoot -Arguments @('systemctl', 'daemon-reload')
    $restart = Invoke-PpWslExec -AsRoot -Arguments @('systemctl', 'restart', 'docker')
    return ($mkdir.ExitCode -eq 0 -and $install.ExitCode -eq 0 -and $reload.ExitCode -eq 0 -and $restart.ExitCode -eq 0)
}

# ---------------------------------------------------------------------------
$profile = Read-PpProfile -Path $ProfilePath
if (Get-PpValue $profile 'PP_WSL_DISTRO' '') { $WslDistro = Get-PpValue $profile 'PP_WSL_DISTRO' }
if (Get-PpValue $profile 'PP_PROXY_URL' '') { $ProxyUrl = Get-PpValue $profile 'PP_PROXY_URL' }
if ($WslDistro -notmatch '^[A-Za-z0-9._-]+$') {
    throw 'preflight_windows_node_wsl_distro_invalid'
}
if ($ProxyUrl) {
    $configuredProxyUri = $null
    if (
        -not [System.Uri]::TryCreate($ProxyUrl, [System.UriKind]::Absolute, [ref]$configuredProxyUri) -or
        $configuredProxyUri.Scheme -notin @('http', 'https') -or
        $configuredProxyUri.UserInfo
    ) {
        throw 'preflight_windows_node_proxy_url_invalid'
    }
}

$report = [ordered]@{}

# 1. docker runtime detection
$desktopDocker = Test-PpDesktopDocker
$wslDocker = Test-PpWslDocker
if ($desktopDocker) {
    $report.docker_runtime = 'desktop'
    $report.docker_context = Get-PpActiveDockerContext
    $report.docker_command = 'docker.exe'
    $probe = Invoke-PpDockerQuiet -Arguments @('--context', 'desktop-linux', 'version', '--format', '{{.Server.Version}}')
    $report.docker_version = $probe.Output
    Write-PpLog "docker runtime: Docker Desktop ($($report.docker_version))"
}
elseif ($wslDocker) {
    $report.docker_runtime = 'wsl-native'
    $report.docker_version = Get-PpWslDockerVersion
    # Reliable across NAT/mirrored networks: run docker through the WSL
    # command prefix. The TCP bridge is only an optional convenience and is
    # never required by the build/verify stages.
    $report.docker_command = "wsl.exe -d $WslDistro -e docker"
    Write-PpLog "docker runtime: WSL2 native ($($report.docker_version)) in $WslDistro"
    if ($EnableDockerBridge) {
        $bridge = Ensure-PpSocatBridge -Profile $profile
        $report.docker_context = Get-PpActiveDockerContext
        $report.docker_bridge_connected = $bridge.Connected
        $report.docker_cli_available = $false
        if (-not $bridge.Connected) {
            Write-PpLog "WSL docker bridge not connected: $($bridge.Reason)"
            Write-PpLog 'use the WSL command prefix for docker: wsl -d <distro> -e docker <cmd>'
        }
    }
    else {
        $report.docker_context = ''
        $report.docker_bridge_connected = $false
        $report.docker_cli_available = $false
    }
}
else {
    Write-PpLog 'no Docker runtime detected (Docker Desktop off and WSL native daemon missing)'
    $report.docker_runtime = 'none'
    $report.docker_context = ''
    $report.docker_version = ''
    $report.docker_bridge_connected = $false
    $report.docker_cli_available = $false
    $report.docker_command = ''
}

# 2. system disk headroom + VHDX placement
$disk = Test-PpSystemDiskHeadroom
$report.system_disk_free_gb = $disk.FreeGb
$report.vhdx_on_system_disk = $disk.VhdxOnSystemDisk
$report.vhdx_path = $disk.VhdxPath
$report.disk_ok = $disk.Ok
Write-PpLog $disk.Message
if ($MigrateVhdxTo -and $disk.VhdxOnSystemDisk) {
    $migration = Invoke-PpVhdxMigration -Target $MigrateVhdxTo
    $report.vhdx_migration = $migration
    if ($migration.Migrated) {
        $disk = Test-PpSystemDiskHeadroom
        $report.system_disk_free_gb = $disk.FreeGb
        $report.vhdx_on_system_disk = $disk.VhdxOnSystemDisk
        $report.vhdx_path = $disk.VhdxPath
    }
}
elseif (-not $disk.Ok -and $disk.VhdxOnSystemDisk) {
    Write-PpLog "recommended: wsl --manage $WslDistro --move D:\WSL (or pass -MigrateVhdxTo)"
}

# 3/4. WSL resource + systemd configuration is required only when the selected
# runtime is the native daemon. Docker Desktop owns its own WSL lifecycle.
if ($report.docker_runtime -eq 'wsl-native') {
    $wslConfig = Ensure-PpWslConfig -Profile $profile
    $report.wslconfig = [ordered]@{
        path = $wslConfig.Path
        changed = $wslConfig.Changed
        needs_restart = $wslConfig.NeedsRestart
        memory = $wslConfig.Memory
        processors = $wslConfig.Processors
        swap = $wslConfig.Swap
    }
    if ($wslConfig.Changed) {
        Write-PpLog ".wslconfig updated at $($wslConfig.Path); run 'wsl --shutdown' once to apply"
    }

    $wslSystemdConfig = Ensure-PpWslSystemdConfig
    $report.wsl_systemd_config = [ordered]@{
        changed = $wslSystemdConfig.Changed
        installed = $wslSystemdConfig.Installed
        needs_restart = $wslSystemdConfig.Changed
        reason = $wslSystemdConfig.Reason
    }
    if ($wslSystemdConfig.Changed) {
        Write-PpLog "/etc/wsl.conf updated with [boot] systemd=true; run 'wsl --shutdown' once to apply"
    }

    $sysd = Test-PpSystemdDocker
    $report.systemd = [ordered]@{
        state = $sysd.SystemdState
        docker_enabled = $sysd.DockerEnabled
        docker_active = $sysd.DockerActive
        needs_wsl_restart = $sysd.NeedsWslRestart
    }
    Write-PpLog "systemd: $($sysd.SystemdState); docker enabled: $($sysd.DockerEnabled); active: $($sysd.DockerActive)"
}
else {
    $report.wslconfig = [ordered]@{ changed = $false; needs_restart = $false; skipped = $true }
    $report.wsl_systemd_config = [ordered]@{ changed = $false; installed = $true; needs_restart = $false; reason = 'desktop-runtime' }
    $report.systemd = [ordered]@{ state = 'desktop-managed'; docker_enabled = $true; docker_active = $true; needs_wsl_restart = $false }
}

# 5. proxy detection
$proxy = Test-PpProxy -UseWsl:($report.docker_runtime -eq 'wsl-native')
$report.direct_connectivity = $proxy.DirectOk
$report.proxy_url = $proxy.ProxyUrl
if (-not $proxy.DirectOk) {
    if ($proxy.ProxyUrl) {
        $written = if ($report.docker_runtime -eq 'wsl-native') {
            Write-PpWslProxyProfile -ProxyUrl $proxy.ProxyUrl
        }
        else { $false }
        $dockerProxyWritten = if ($report.docker_runtime -eq 'wsl-native') {
            Write-PpDockerProxyDropIn -ProxyUrl $proxy.ProxyUrl
        }
        else { $false }
        $report.proxy_profile_written = $written
        $report.docker_proxy_configured = $dockerProxyWritten
        Write-PpLog "direct connectivity failed; using a reachable proxy (wsl profile: $written; docker service: $dockerProxyWritten)"
    }
    else {
        $report.proxy_profile_written = $false
        $report.docker_proxy_configured = $false
        Write-PpLog 'direct connectivity failed and no local proxy found; WSL downloads may need a proxy'
    }
}
else {
    $report.proxy_profile_written = $false
    $report.docker_proxy_configured = $false
    Write-PpLog 'direct connectivity ok'
}

$blockingReasons = New-Object System.Collections.Generic.List[string]
if ($report.docker_runtime -eq 'none') {
    $blockingReasons.Add('docker-runtime-missing')
}
if ($report.docker_runtime -eq 'wsl-native' -and -not $report.disk_ok -and $report.vhdx_on_system_disk) {
    $blockingReasons.Add('system-disk-low-with-wsl-vhdx')
}
if ($MigrateVhdxTo -and $report.Contains('vhdx_migration') -and -not $report.vhdx_migration.Migrated) {
    $blockingReasons.Add('vhdx-migration-failed')
}
if (-not $report.wsl_systemd_config.installed) {
    $blockingReasons.Add('wsl-systemd-config-write-failed')
}
if (-not $report.systemd.docker_enabled -or -not $report.systemd.docker_active) {
    $blockingReasons.Add('wsl-docker-service-not-ready')
}
if ($report.docker_runtime -eq 'wsl-native' -and -not $report.direct_connectivity -and $report.proxy_url -and -not $report.docker_proxy_configured) {
    $blockingReasons.Add('wsl-docker-proxy-config-failed')
}
if (-not $report.direct_connectivity -and -not $report.proxy_url) {
    $blockingReasons.Add('network-path-unavailable')
}
$report.ready = ($blockingReasons.Count -eq 0)
$report.blocking_reasons = @($blockingReasons)

$reportJson = $report | ConvertTo-Json -Depth 4
if ($OutputPath) {
    $outputDir = Split-Path -Parent $OutputPath
    if ($OutputPath -and -not $outputDir) { $outputDir = (Get-Location).Path }
    if ($outputDir -and -not (Test-Path -LiteralPath $outputDir)) {
        New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    }
    [System.IO.File]::WriteAllText($OutputPath, $reportJson, [System.Text.UTF8Encoding]::new($false))
    Write-PpLog "report written to $OutputPath"
}
Write-Output $reportJson

if ($blockingReasons.Count -gt 0) {
    Write-PpLog ('host not ready: ' + ($blockingReasons -join ', '))
    exit 1
}
exit 0
