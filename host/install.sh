#!/bin/bash
#
# Install the Mac side: compile the agent-pr-review:// URL shim into an app bundle
# and register the scheme with Launch Services.
#
# This script is host-only on purpose. It does NOT deploy anything into the VM.
# The VM launcher is installed from the VM's own checkout with vm/install.sh, so
# each side has exactly one source of truth. (An earlier version scp'd the launcher
# from this checkout into the guest, which meant a stale Mac checkout could silently
# overwrite a newer VM launcher.)
set -e

APP_NAME="AgentPRReview"
LEGACY_APP_NAMES=("GitHubPRReview" "ClaudeReview")
APP_DIR="$HOME/Applications/${APP_NAME}.app"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$SCRIPT_DIR/../extension/manifest.json")"

# Validate configuration and generate host permissions before changing the install.
EXTENSION_DIR="${AGENT_PR_REVIEW_EXTENSION_DIR:-$HOME/.local/share/agent-pr-review/extension}"
python3 "$SCRIPT_DIR/../tools/build-extension.py" --output "$EXTENSION_DIR"

echo "=== Agent PR Review - Mac URL handler installer ==="
echo ""

rm -rf "${APP_DIR}"
for legacy in "${LEGACY_APP_NAMES[@]}"; do
  rm -rf "$HOME/Applications/${legacy}.app"
done

echo "Creating ${APP_NAME}.app in ~/Applications/ ..."
mkdir -p "${APP_DIR}/Contents/MacOS"

cat > "${APP_DIR}/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>
  <string>com.agent-pr-review.handler</string>
  <key>CFBundleName</key>
  <string>AgentPRReview</string>
  <key>CFBundleDisplayName</key>
  <string>Agent PR Review</string>
  <key>CFBundleVersion</key>
  <string>1.0.0</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0.0</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleExecutable</key>
  <string>AgentPRReview</string>
  <key>LSBackgroundOnly</key>
  <true/>
  <key>CFBundleURLTypes</key>
  <array>
    <dict>
      <key>CFBundleURLName</key>
      <string>Agent PR Review URL</string>
      <key>CFBundleURLSchemes</key>
      <array>
        <string>agent-pr-review</string>
      </array>
    </dict>
  </array>
</dict>
</plist>
PLIST

/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $APP_VERSION" "${APP_DIR}/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $APP_VERSION" "${APP_DIR}/Contents/Info.plist"

# The app resolves the user home through Foundation, including Launch Services.
echo "Compiling URL shim ..."
swiftc -o "${APP_DIR}/Contents/MacOS/AgentPRReview" "${SCRIPT_DIR}/ClaudeReviewHandler.swift" -framework Cocoa

echo "Registering agent-pr-review:// URL scheme ..."
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -R "${APP_DIR}"

python3 "$SCRIPT_DIR/install-native.py" --extension-dir "$EXTENSION_DIR"

echo ""
echo "Done. Installed on this Mac:"
echo "  - ${APP_DIR} (URL scheme handler)"
echo "  - agent-pr-review:// URL scheme registered"
echo ""
echo "Next steps:"
echo "  1. In the VM, install the launcher that does the actual work:"
echo "       ssh <your-dev-host> -- code/agent-pr-review/vm/install.sh"
echo "  2. Load the Chrome extension from $EXTENSION_DIR"
echo "     (chrome://extensions > Developer mode > Load unpacked)"
echo "  3. Open any GitHub PR and use the review launcher in the PR header"
echo ""
echo "To uninstall on this Mac:"
echo "  rm -rf '${APP_DIR}'"
