#!/bin/bash
# Patches the Electron app bundle with Ki name and icon.
# Runs automatically via postinstall; re-run manually after npm install.
set -e
BUNDLE="$(dirname "$0")/node_modules/electron/dist/Electron.app"
PLIST="$BUNDLE/Contents/Info.plist"

/usr/libexec/PlistBuddy -c "Set :CFBundleName Ki" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName Ki" "$PLIST"

DIR="$(dirname "$0")"
mkdir -p "$DIR/ki.iconset"
for size in 16 32 64 128 256 512 1024; do
  sips -z $size $size "$DIR/icon.png" --out "$DIR/ki.iconset/icon_${size}x${size}.png" > /dev/null 2>&1
done
cp "$DIR/ki.iconset/icon_32x32.png"     "$DIR/ki.iconset/icon_16x16@2x.png"
cp "$DIR/ki.iconset/icon_64x64.png"     "$DIR/ki.iconset/icon_32x32@2x.png"
cp "$DIR/ki.iconset/icon_256x256.png"   "$DIR/ki.iconset/icon_128x128@2x.png"
cp "$DIR/ki.iconset/icon_512x512.png"   "$DIR/ki.iconset/icon_256x256@2x.png"
cp "$DIR/ki.iconset/icon_1024x1024.png" "$DIR/ki.iconset/icon_512x512@2x.png"
iconutil -c icns "$DIR/ki.iconset" -o "$DIR/ki.icns"
cp "$DIR/ki.icns" "$BUNDLE/Contents/Resources/electron.icns"
rm -rf "$DIR/ki.iconset" "$DIR/ki.icns"

echo "Electron bundle patched: name=Ki, icon updated"
