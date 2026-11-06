#!/usr/bin/env bash
# Build release APK and upload to Firebase App Distribution.
# Participants in the "participants" tester group get an email + push notification.
#
# Usage:
#   ./release.sh                     # prompts for release notes
#   ./release.sh "fixed upload bug"  # uses given message as release notes

set -euo pipefail

cd "$(dirname "$0")"

if [ $# -ge 1 ]; then
    echo "$1" > release-notes.txt
else
    echo "Enter release notes (what changed in this version). End with Ctrl-D:"
    cat > release-notes.txt
fi

echo ""
echo "Release notes:"
cat release-notes.txt
echo ""
echo "Building + uploading..."

./gradlew clean assembleDebug appDistributionUploadDebug

echo ""
echo "Done. Participants will get an email + push notification."
