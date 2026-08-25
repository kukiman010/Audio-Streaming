#!/usr/bin/env bash
set -euo pipefail

VARIANT="release"
PULL=0

usage() {
    cat <<'EOF'
Usage: ./build_docker.sh [--variant debug|release] [--pull]

Builds the Android APK using ghcr.io/cirruslabs/android-sdk:36-ndk.
Gradle downloads are cached in Docker volume: audio-streaming-gradle-cache
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --variant)
            VARIANT="${2:-}"
            shift 2
            ;;
        --pull)
            PULL=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ "$VARIANT" != "debug" && "$VARIANT" != "release" ]]; then
    echo "Invalid variant: $VARIANT (expected debug or release)" >&2
    exit 1
fi

ANDROID_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_IMAGE="ghcr.io/cirruslabs/android-sdk:36-ndk"
GRADLE_CACHE_VOLUME="audio-streaming-gradle-cache"
GRADLE_TASK="assembleRelease"
APK_NAME="app-release.apk"

if [[ "$VARIANT" == "debug" ]]; then
    GRADLE_TASK="assembleDebug"
    APK_NAME="app-debug.apk"
fi

APK_PATH="$ANDROID_DIR/app/build/outputs/apk/$VARIANT/$APK_NAME"

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed or not in PATH." >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "Docker is installed but not running." >&2
    exit 1
fi

if [[ "$PULL" -eq 1 ]] || ! docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
    echo "Pulling builder image $DOCKER_IMAGE..."
    docker pull "$DOCKER_IMAGE"
else
    echo "Using local builder image $DOCKER_IMAGE"
fi

echo "Using Gradle cache volume: $GRADLE_CACHE_VOLUME"
echo "Building Android APK ($VARIANT) in Docker..."

docker run --rm \
    -e GRADLE_USER_HOME=/root/.gradle \
    -v "$GRADLE_CACHE_VOLUME:/root/.gradle" \
    -v "$ANDROID_DIR:/workspace" \
    -w /workspace \
    "$DOCKER_IMAGE" \
    bash -lc "set -euo pipefail && cd /workspace && sed -i 's/\r$//' gradlew && chmod +x gradlew && ./gradlew $GRADLE_TASK --no-daemon"

if [[ ! -f "$APK_PATH" ]]; then
    echo "Build finished but APK was not found at: $APK_PATH" >&2
    exit 1
fi

echo
echo "Build succeeded."
echo "APK: $APK_PATH"
echo
echo "Gradle cache volume: $GRADLE_CACHE_VOLUME"
echo "Clear cache if needed: docker volume rm $GRADLE_CACHE_VOLUME"
echo
echo "Install on a connected device:"
echo "  adb install -r \"$APK_PATH\""
