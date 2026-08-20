[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("dry-run", "release")]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [string]$BuildDirectory,

    [Parameter(Mandatory = $true)]
    [string]$ReleaseDirectory,

    [string]$Tag,

    [Parameter(Mandatory = $true)]
    [string]$Commit,

    [string]$Timestamp
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Version -notmatch "^[0-9]+\.[0-9]+\.[0-9]+$") {
    throw "Version must match X.Y.Z exactly: $Version"
}
if ($Commit -notmatch "^[0-9a-fA-F]{40}$") {
    throw "Commit must be a full 40-character hexadecimal SHA"
}

if ($Mode -eq "release") {
    if ([string]::IsNullOrWhiteSpace($Tag) -or $Tag -notmatch "^v([0-9]+\.[0-9]+\.[0-9]+)$") {
        throw "Release mode requires an exact vX.Y.Z tag"
    }
    if ($Matches[1] -ne $Version) {
        throw "Release tag/version mismatch: $Tag versus $Version"
    }
} elseif (-not [string]::IsNullOrWhiteSpace($Tag)) {
    throw "Dry-run mode cannot carry a Git tag"
}

$root = (Get-Location).Path
$buildPath = [IO.Path]::GetFullPath((Join-Path $root $BuildDirectory))
$releasePath = [IO.Path]::GetFullPath((Join-Path $root $ReleaseDirectory))
$executablePath = Join-Path $buildPath "Release\coreguard.exe"
$artifactName = "coreguard-$Version-windows-x64-msvc.zip"
$artifactPath = Join-Path $releasePath $artifactName
$trustScript = Join-Path $root "scripts\release_trust.py"

$vsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $vsWhere)) {
    throw "vswhere.exe was not found: $vsWhere"
}
$global:LASTEXITCODE = 0
$vsInstall = (& $vsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath |
    Select-Object -First 1)
if ($global:LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($vsInstall)) {
    throw "MSVC x64 installation was not found"
}
$vsInstall = $vsInstall.Trim()
$vsDevCmd = Join-Path $vsInstall "Common7\Tools\VsDevCmd.bat"
if (-not (Test-Path -LiteralPath $vsDevCmd)) {
    throw "VsDevCmd.bat was not found: $vsDevCmd"
}

function Invoke-DeveloperCommand {
    param([Parameter(Mandatory = $true)][string]$CommandLine)

    Write-Host "Executing MSVC developer command: $CommandLine"
    $global:LASTEXITCODE = 0
    & $env:ComSpec /d /s /c $CommandLine
    if ($global:LASTEXITCODE -ne 0) {
        throw "MSVC developer command failed with exit code $global:LASTEXITCODE"
    }
}

function Invoke-PythonGate {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    Write-Host ("Executing release trust gate: python " + ($Arguments -join " "))
    $global:LASTEXITCODE = 0
    & python $trustScript @Arguments
    if ($global:LASTEXITCODE -ne 0) {
        throw "Release trust gate failed with exit code $global:LASTEXITCODE"
    }
}

$developerCommand = @(
    ('call "' + $vsDevCmd + '" -arch=x64 -host_arch=x64'),
    ('cmake -S "' + $root + '" -B "' + $buildPath + '" -A x64 -DCOREGUARD_VERSION=' + $Version),
    ('cmake --build "' + $buildPath + '" --config Release --parallel --verbose'),
    'python -m unittest discover -s tests -p "test_*.py" -v',
    ('python tests\verification.py --exe "' + $executablePath + '"'),
    ('cd /d "' + $buildPath + '" && cpack --config "' + (Join-Path $buildPath "CPackConfig.cmake") + '" -C Release')
) -join " && "

Write-Host "Coreguard release trust mode: $Mode"
Write-Host "Coreguard validation version: $Version"
Invoke-DeveloperCommand $developerCommand

if (-not (Test-Path -LiteralPath $executablePath)) {
    throw "Release executable was not produced: $executablePath"
}
$cpackArtifact = Join-Path $buildPath $artifactName
if (-not (Test-Path -LiteralPath $cpackArtifact)) {
    throw "CPack artifact was not produced: $cpackArtifact"
}
New-Item -ItemType Directory -Path $releasePath -Force | Out-Null
Copy-Item -LiteralPath $cpackArtifact -Destination $artifactPath -Force

Invoke-PythonGate @("validate-version", "--version", $Version)
if ($Mode -eq "release") {
    Invoke-PythonGate @(
        "validate-build", "--tag", $Tag, "--build-dir", $buildPath, "--artifact", $artifactPath
    )
} else {
    Invoke-PythonGate @(
        "validate-build", "--version", $Version, "--build-dir", $buildPath, "--artifact", $artifactPath
    )
}
Invoke-PythonGate @("validate-package", "--artifact", $artifactPath, "--version", $Version)

$evidenceArguments = @("write-evidence", "--artifact", $artifactPath, "--commit", $Commit, "--output-dir", $releasePath)
if ($Mode -eq "release") {
    $evidenceArguments += @("--tag", $Tag)
} else {
    $evidenceArguments += @("--version", $Version, "--dry-run")
}
if (-not [string]::IsNullOrWhiteSpace($Timestamp)) {
    $evidenceArguments += @("--timestamp", $Timestamp)
}
Invoke-PythonGate $evidenceArguments
Invoke-PythonGate @(
    "verify-sha256sums", "--checksums", (Join-Path $releasePath "SHA256SUMS"), "--artifact", $artifactPath
)
$manifestArguments = @(
    "verify-manifest",
    "--manifest", (Join-Path $releasePath "release-manifest.json"),
    "--checksums", (Join-Path $releasePath "SHA256SUMS"),
    "--artifact", $artifactPath,
    "--version", $Version,
    "--commit", $Commit
)
if ($Mode -eq "dry-run") {
    $manifestArguments += "--dry-run"
}
Invoke-PythonGate $manifestArguments

Write-Host "Release trust package and evidence gates passed."
Write-Host "Artifact: $artifactPath"
Write-Host "SHA256SUMS: $(Join-Path $releasePath 'SHA256SUMS')"
Write-Host "Manifest: $(Join-Path $releasePath 'release-manifest.json')"
