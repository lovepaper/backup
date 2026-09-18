# NetSwitch

跨平台网络控制开关，给测试工作用。一键整机断网 / 循环断网 / 单应用断网 / 弱网模拟。

## 功能

| 模式 | 说明 | Windows | macOS |
|---|---|:---:|:---:|
| 一 · 整机断网 | 禁用/启用网卡 | ✅ | ✅ |
| 二 · 循环断网 | 断 X 秒→连 Y 秒，模拟抖动 | ✅ | ✅ |
| 三 · 单应用断网 | 只掐一个 exe 的网络 | ✅ | ❌（系统限制） |
| 四 · 弱网模拟 | 带宽/延迟/丢包/抖动/乱序 | ✅ WinDivert | ✅ dummynet |

> macOS dummynet 只支持带宽/延迟/丢包；抖动、乱序仅 Windows 有效。

## 平台

- **Windows**：需管理员，`NetSwitch.exe`（`netswitch/` 目录下，或重新打包）
- **macOS**：需 root，`NetSwitch.app`（`NetSwitch.dmg`，Release `netswitch-macos`，未签名）

## 下载

- **macOS DMG**（未签名）：https://github.com/lovepaper/backup/releases/download/netswitch-macos/NetSwitch.dmg
  （Release 页面：https://github.com/lovepaper/backup/releases/tag/netswitch-macos）
- **Windows exe**：见仓库 `netswitch/NetSwitch.exe`

> 公开仓库，`netswitch/` 子目录含全部源码与脚本，可 `git clone` 后按"构建"一节本地打包。

## 弱网实现

- Windows：`pydivert`（自带 WinDivert 驱动），系统级对所有包限速/延迟/丢包/乱序
- macOS：`pfctl` + `dnctl`（dummynet），系统原生，无需额外工具

## 构建

```bash
# Windows（带 tkinter 的 Python）
pyinstaller --noconfirm --onefile --windowed --name NetSwitch ^
  --hidden-import=keyboard --hidden-import=win32com.client --hidden-import=pydivert netswitch.py

# macOS（本机 Mac）
bash build_mac.sh
bash create_dmg.sh
```

或用 GitHub Actions 自动出 DMG（`.github/workflows/build-mac.yml`，产物在 Release）。

## 注意

- 弱网对所有流量生效，停止后恢复。
- DMG 为 ad-hoc 签名（无 Apple 开发者证书）：首次打开右键 → 打开即可；若仍被拦，`xattr -dr com.apple.quarantine /Applications/NetSwitch.app`。
- 详细使用见 [使用说明.md](使用说明.md)。

版本 v2.0.0 · Python 3.11 + tkinter

<!-- intel-build 2026-09-18 14:37 -->
