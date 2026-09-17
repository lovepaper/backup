#!/usr/bin/env bash
# NetSwitch macOS 打包脚本（在本机 Mac 上运行）
# 用法： bash build_mac.sh
# 注意：CI 中由 setup-python 的 Python 运行（PYTHON 环境变量传入），用 venv 隔离依赖
# CI build: 用 --system-site-packages venv 继承系统已装的 pyinstaller / tkinter
set -e
cd "$(dirname "$0")"

APP_NAME=NetSwitch
PYTHON="${PYTHON:-python3}"

echo "==> 基础 python: $PYTHON -> $("$PYTHON" --version 2>&1)"

echo "==> 创建虚拟环境（隔离 pyinstaller，避免系统/用户 site 路径打架）"
VENV="build/venv"
"$PYTHON" -m venv --system-site-packages "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
echo "venv python: $(which python)"
echo "venv pip: $(python -m pip --version)"

echo "==> 安装依赖"
python -m pip install --upgrade pip
# 关键：必须强制装进 venv 自己的 site-packages，否则 venv 运行时找不到系统已有的 pyinstaller
python -m pip install --force-reinstall --no-deps pyinstaller
python -c "import tkinter; print('tkinter OK')"

echo "==> PyInstaller 打包 .app"
# 注意：macOS 文件系统大小写敏感，pyinstaller 包名实为 PyInstaller（大写）。
# 不能用 `python -m pyinstaller`（小写找不到），必须用 venv 的 pyinstaller 命令。
pyinstaller --noconfirm --windowed --name "$APP_NAME" \
  --osx-bundle-identifier com.netswitch.app \
  --exclude-module windivert_throttle --exclude-module pydivert \
  --exclude-module win32com --exclude-module win32api --exclude-module win32gui \
  --exclude-module win32event --exclude-module win32process --exclude-module pywin32 \
  --exclude-module keyboard \
  netswitch.py

echo "==> 清理扩展属性（避免 quarantine 误判导致 🚫）"
xattr -cr "dist/$APP_NAME.app" || true

echo "==> 确保可执行位"
chmod +x "dist/$APP_NAME.app/Contents/MacOS/$APP_NAME" 2>/dev/null || true

echo "==> ad-hoc 签名（无 Apple 证书也能让 Gatekeeper 接受，消除『已损坏/🚫』）"
codesign --force --deep --sign - "dist/$APP_NAME.app" || true
echo "==> 签名校验："
codesign -vvv "dist/$APP_NAME.app" 2>&1 || true
spctl --assess -vv "dist/$APP_NAME.app" 2>&1 || true

echo "==> 完成：dist/$APP_NAME.app"
