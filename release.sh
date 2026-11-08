#!/usr/bin/env bash
# Build debug APK and upload to Firebase App Distribution.
#
# Usage:
#   ./release.sh "release notes"                        # → participants group (default)
#   ./release.sh --to researchers "release notes"       # → researchers group only
#   ./release.sh --to all "release notes"               # → everyone
#
# Groups in Firebase console:
#   participants  — real TCU study participants
#   researchers   — TEST-role researcher mock accounts
#   all           — everyone (participants + researchers)
#
# Bump versionCode in app/build.gradle.kts before each release, or Firebase
# treats it as a re-upload and testers won't get a new-release notification.

set -euo pipefail

cd "$(dirname "$0")"

GROUP="participants"
NOTES=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --to)
            GROUP="$2"
            shift 2
            ;;
        *)
            NOTES="$1"
            shift
            ;;
    esac
done

if [[ "$GROUP" != "participants" && "$GROUP" != "researchers" && "$GROUP" != "all" ]]; then
    echo "Error: --to must be participants, researchers, or all" >&2
    exit 1
fi

if [[ -n "$NOTES" ]]; then
    echo "$NOTES" > release-notes.txt
else
    echo "Enter release notes (what changed in this version). End with Ctrl-D:"
    cat > release-notes.txt
fi

echo ""
echo "Target group : $GROUP"
echo "Release notes: $(cat release-notes.txt)"
echo ""
echo "Building + uploading..."

./gradlew clean assembleDebug appDistributionUploadDebug -PdistGroup="$GROUP"

echo ""
echo "Done. '$GROUP' testers will get an email + push notification."
