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

- **Windows**：需管理员，`NetSwitch.exe`（见仓库根目录或 Release）
- **macOS**：需 root，`NetSwitch.app`（Release 里的 `NetSwitch.dmg`，未签名）

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
- DMG 未签名：首次打开右键 → 打开，或 `xattr -d com.apple.quarantine /Applications/NetSwitch.app`。
- 详细使用见 [使用说明.md](使用说明.md)。

版本 v2.0.0 · Python 3.11 + tkinter
