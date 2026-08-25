param(
    [ValidateSet("debug", "release")]
    [string]$Variant = "release",
    [switch]$Pull
)

$ErrorActionPreference = "Stop"

$androidDir = $PSScriptRoot
$dockerImage = "ghcr.io/cirruslabs/android-sdk:36-ndk"
$gradleCacheVolume = "audio-streaming-gradle-cache"
$gradleTask = if ($Variant -eq "release") { "assembleRelease" } else { "assembleDebug" }
$apkName = if ($Variant -eq "release") { "app-release.apk" } else { "app-debug.apk" }
$apkPath = Join-Path $androidDir "app\build\outputs\apk\$Variant\$apkName"

function Resolve-DockerExe {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if ($docker) {
        return $docker.Source
    }

    $commonPaths = @(
        "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe",
        "${env:ProgramFiles(x86)}\Docker\Docker\resources\bin\docker.exe"
    )

    foreach ($path in $commonPaths) {
        if (Test-Path $path) {
            return $path
        }
    }

    return $null
}

$dockerExe = Resolve-DockerExe
if (-not $dockerExe) {
    throw @"
Docker is not installed or not running.

Install Docker Desktop for Windows:
  https://www.docker.com/products/docker-desktop/

Then run:
  .\build_docker.bat -Variant release
"@
}

& $dockerExe info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker is installed but not running. Start Docker Desktop and retry."
}

$imageExists = & $dockerExe image inspect $dockerImage 2>$null
if ($Pull -or -not $imageExists) {
    Write-Host "Pulling builder image $dockerImage..."
    & $dockerExe pull $dockerImage
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to pull Docker image $dockerImage"
    }
} else {
    Write-Host "Using local builder image $dockerImage"
}

# Script does not install anything on Windows.
# Inside Docker it only runs: chmod gradlew && ./gradlew <task>
# Downloads are cached in the named Docker volume below.
Write-Host "Using Gradle cache volume: $gradleCacheVolume"
Write-Host "Building Android APK ($Variant) in Docker..."

$buildCommand = "set -euo pipefail && cd /workspace && sed -i 's/\r`$//' gradlew && chmod +x gradlew && ./gradlew $gradleTask --no-daemon"

& $dockerExe run --rm `
    -e GRADLE_USER_HOME=/root/.gradle `
    -v "${gradleCacheVolume}:/root/.gradle" `
    -v "${androidDir}:/workspace" `
    -w /workspace `
    $dockerImage `
    bash -lc "$buildCommand"

if ($LASTEXITCODE -ne 0) {
    throw "Gradle build in Docker failed with exit code $LASTEXITCODE"
}

if (-not (Test-Path $apkPath)) {
    throw "Build finished but APK was not found at: $apkPath"
}

$apkPathForAdb = $apkPath.Replace('\', '/')

Write-Host ""
Write-Host "Build succeeded."
Write-Host "APK: $apkPath"
Write-Host ""
Write-Host "Gradle cache volume: $gradleCacheVolume"
Write-Host "Clear cache if needed: docker volume rm $gradleCacheVolume"
Write-Host ""
Write-Host "Copy from Explorer or install via USB/adb."
Write-Host "Install on a connected device (keep quotes):"
Write-Host "  adb install -r `"$apkPathForAdb`""
