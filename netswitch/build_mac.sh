#!/usr/bin/env bash
# NetSwitch macOS 打包脚本（在本机 Mac 上运行）
# 用法： bash build_mac.sh
set -e
cd "$(dirname "$0")"

APP_NAME=NetSwitch
PYTHON="${PYTHON:-python3}"

echo "==> 安装依赖"
"$PYTHON" -m pip install --upgrade pip pyinstaller
"$PYTHON" -c "import tkinter; print('tkinter OK')"

echo "==> PyInstaller 打包 .app"
"$PYTHON" -m pyinstaller --noconfirm --windowed --name "$APP_NAME" \
  --osx-bundle-identifier com.netswitch.app \
  --exclude-module windivert_throttle --exclude-module pydivert \
  netswitch.py

echo "==> 完成：dist/$APP_NAME.app"
