#!/usr/bin/env bash
# NetSwitch macOS DMG 打包脚本（在本机 Mac 上运行，需先 build_mac.sh）
# 用法： bash create_dmg.sh
set -e
cd "$(dirname "$0")"

APP_NAME=NetSwitch
APP="dist/$APP_NAME.app"
DMG="$APP_NAME.dmg"
STAGE="dist/dmg_stage"

if [ ! -d "$APP" ]; then
  echo "!! 找不到 $APP，请先运行 build_mac.sh" >&2
  exit 1
fi

rm -f "$DMG"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

echo "==> hdiutil 生成 $DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

echo "==> 完成： $DMG ($(du -h "$DMG" | cut -f1))"
