param(
    [switch]$InstallDeps,
    [switch]$SkipInstaller,
    [switch]$CleanInstallersOnly,
    [switch]$NoVendorSdk,
    [string]$SdkX64Path = "D:\HuaTengVision\SDK\X64",
    [string]$InnoSetupCompiler = ""
)

$ErrorActionPreference = "Stop"

function Get-InnoDefine {
    param(
        [string[]]$Lines,
        [string]$Name
    )

    $match = $Lines | Select-String -Pattern "^\s*#define\s+$Name\s+`"([^`"]+)`"" | Select-Object -First 1
    if (-not $match) {
        throw "Inno Setup define not found: $Name"
    }

    return $match.Matches[0].Groups[1].Value
}

function Get-InnoSetting {
    param(
        [string[]]$Lines,
        [string]$Name
    )

    $match = $Lines | Select-String -Pattern "^\s*$Name=(.+)\s*$" | Select-Object -First 1
    if (-not $match) {
        throw "Inno Setup setting not found: $Name"
    }

    return $match.Matches[0].Groups[1].Value.Trim()
}

function Clear-OldInstallers {
    param(
        [string]$ReleaseDir,
        [string]$CurrentInstallerName,
        [string]$InstallerFilter
    )

    if (-not (Test-Path -LiteralPath $ReleaseDir)) {
        Write-Host "Release directory does not exist: $ReleaseDir"
        return
    }

    $releaseRoot = (Resolve-Path -LiteralPath $ReleaseDir).Path
    $currentInstallerPath = Join-Path $ReleaseDir $CurrentInstallerName
    if (-not (Test-Path -LiteralPath $currentInstallerPath)) {
        throw "Current installer was not found: $currentInstallerPath"
    }

    $currentResolvedPath = (Resolve-Path -LiteralPath $currentInstallerPath).Path
    $oldInstallers = Get-ChildItem -LiteralPath $ReleaseDir -File -Filter $InstallerFilter |
        Where-Object { (Resolve-Path -LiteralPath $_.FullName).Path -ne $currentResolvedPath }

    foreach ($installer in $oldInstallers) {
        $targetPath = (Resolve-Path -LiteralPath $installer.FullName).Path
        if ([System.IO.Path]::GetDirectoryName($targetPath) -ne $releaseRoot) {
            throw "Refusing to remove installer outside release directory: $targetPath"
        }

        Remove-Item -LiteralPath $targetPath -Force
        Write-Host "Removed old installer: $($installer.Name)"
    }
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$SpecPath = (Get-ChildItem -Path (Join-Path $ProjectRoot "packaging") -Filter "*.spec" | Select-Object -First 1).FullName
$IssPath = (Get-ChildItem -Path (Join-Path $ProjectRoot "installer") -Filter "*.iss" | Select-Object -First 1).FullName
$DistRoot = Join-Path $ProjectRoot "dist"
$ReleaseDir = Join-Path $ProjectRoot "release"
$IssLines = Get-Content -LiteralPath $IssPath
$AppVersion = Get-InnoDefine -Lines $IssLines -Name "MyAppVersion"
$OutputBaseFilename = Get-InnoSetting -Lines $IssLines -Name "OutputBaseFilename"
$CurrentInstallerName = ($OutputBaseFilename -replace "\{#MyAppVersion\}", $AppVersion) + ".exe"
$InstallerFilter = ($OutputBaseFilename -replace "\{#MyAppVersion\}", "*") + ".exe"

Set-Location $ProjectRoot

if ($CleanInstallersOnly) {
    Clear-OldInstallers -ReleaseDir $ReleaseDir -CurrentInstallerName $CurrentInstallerName -InstallerFilter $InstallerFilter
    Write-Host "Current installer kept: $CurrentInstallerName"
    exit 0
}

if ($InstallDeps) {
    python -m pip install -r requirements.txt
    python -m pip install pyinstaller
}

python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: .\scripts\build_installer.ps1 -InstallDeps"
}

if (-not $NoVendorSdk) {
    if (-not (Test-Path $SdkX64Path)) {
        throw "HTGE SDK X64 directory not found: $SdkX64Path. Use -NoVendorSdk to skip vendor DLLs."
    }
    $env:HTGE_INCLUDE_SDK = "1"
    $env:HTGE_SDK_X64 = $SdkX64Path
} else {
    $env:HTGE_INCLUDE_SDK = "0"
}

python -m PyInstaller --clean --noconfirm $SpecPath

$DistDir = Get-ChildItem -Path $DistRoot -Directory | Select-Object -First 1
if (-not $DistDir) {
    throw "PyInstaller did not create an application directory under: $DistRoot"
}

if ($SkipInstaller) {
    Write-Host "Portable application directory: $($DistDir.FullName)"
    exit 0
}

if (-not (Test-Path $ReleaseDir)) {
    New-Item -ItemType Directory -Path $ReleaseDir | Out-Null
}

if (-not $InnoSetupCompiler) {
    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($command) {
        $InnoSetupCompiler = $command.Source
    }
}

if (-not $InnoSetupCompiler) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            $InnoSetupCompiler = $candidate
            break
        }
    }
}

if (-not $InnoSetupCompiler -or -not (Test-Path $InnoSetupCompiler)) {
    Write-Warning "Inno Setup compiler ISCC.exe was not found. The portable application was built, but Setup.exe was not created."
    Write-Warning "Install Inno Setup 6 and run this script again, or pass -InnoSetupCompiler."
    exit 0
}

& $InnoSetupCompiler $IssPath
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE"
}

Clear-OldInstallers -ReleaseDir $ReleaseDir -CurrentInstallerName $CurrentInstallerName -InstallerFilter $InstallerFilter

Write-Host "Installer output directory: $ReleaseDir"
