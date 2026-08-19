#!/bin/bash
# Package dist/Magic AI Router.app into a distributable .dmg
# Run AFTER build.sh. VERSION is read from build.sh so there's one source.
# Produces: dist/Magic AI Router-<VERSION>.dmg (with an Applications symlink
# so the user can drag-to-install).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Magic AI Router"
APP="$ROOT/dist/$APP_NAME.app"

VERSION=$(grep -E '^VERSION=' "$ROOT/build.sh" | head -1 | sed -E 's/VERSION="([^"]*)"/\1/')
if [ -z "$VERSION" ]; then
    echo "无法从 build.sh 读取 VERSION"; exit 1
fi

DMG="$ROOT/dist/$APP_NAME-$VERSION.dmg"
TMP_WORK="$(mktemp -d "$ROOT/dist/.dmg-tmp.XXXXXX")"
TMP_DMG="$TMP_WORK/image.dmg"
STAGE=""
MOUNT_DIR=""
ATTACHED=0

cleanup() {
    if [ "$ATTACHED" -eq 1 ] && [ -n "$MOUNT_DIR" ]; then
        hdiutil detach "$MOUNT_DIR" -force >/dev/null 2>&1 || true
    fi
    [ -z "$MOUNT_DIR" ] || rm -rf "$MOUNT_DIR"
    [ -z "$STAGE" ] || rm -rf "$STAGE"
    rm -f "$TMP_DMG"
    rm -rf "$TMP_WORK"
}
trap cleanup EXIT INT TERM

if [ ! -d "$APP" ]; then
    echo "找不到 $APP —— 请先运行 bash build.sh"; exit 1
fi

echo "=== 打包 $APP_NAME v$VERSION → dmg ==="

# staging: .app + Applications 快捷方式(拖拽安装)
STAGE="$(mktemp -d -t magicproxy)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$TMP_DMG" "$DMG"

# 先做一个可读写 dmg,留 20MB 余量给布局元数据
SIZE=$(du -sk "$STAGE" | awk '{print $1}')
SIZE=$((SIZE / 1024 + 20))
[ "$SIZE" -lt 60 ] && SIZE=60
hdiutil create -srcfolder "$STAGE" -volname "$APP_NAME" -fs HFS+ \
    -format UDRW -size "${SIZE}m" "$TMP_DMG" >/dev/null

# 使用唯一挂载目录，避免并行构建或异常中断互相破坏。
MOUNT_DIR="$(mktemp -d -t magicproxy-mount)"
hdiutil attach -readwrite -mountpoint "$MOUNT_DIR" -noverify -noautoopen "$TMP_DMG" >/dev/null
ATTACHED=1

osascript <<APPLESCRIPT 2>/dev/null || true
tell application "Finder"
    tell disk "$APP_NAME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {100, 100, 640, 360}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to 96
        set position of item "$APP_NAME.app" of container window to {140, 130}
        set position of item "Applications" of container window to {420, 130}
        update without registering applications
        delay 1
        close
    end tell
end tell
APPLESCRIPT

hdiutil detach "$MOUNT_DIR" -force >/dev/null 2>&1 || true
ATTACHED=0
rmdir "$MOUNT_DIR" 2>/dev/null || true
MOUNT_DIR=""

# 压缩成只读分发镜像
hdiutil convert "$TMP_DMG" -format UDZO -imagekey zlib-level=9 -o "$DMG" >/dev/null
rm -f "$TMP_DMG"
rm -rf "$STAGE"
STAGE=""

echo ""
echo "=== 完成 ==="
ls -lh "$DMG"
