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
python -m pip show pyinstaller | head -3
python -c "import tkinter; print('tkinter OK')"

echo "==> PyInstaller 打包 .app"
python -m pyinstaller --noconfirm --windowed --name "$APP_NAME" \
  --osx-bundle-identifier com.netswitch.app \
  --exclude-module windivert_throttle --exclude-module pydivert \
  --exclude-module win32com --exclude-module win32api --exclude-module win32gui \
  --exclude-module win32event --exclude-module win32process --exclude-module pywin32 \
  --exclude-module keyboard \
  netswitch.py

echo "==> 完成：dist/$APP_NAME.app"
