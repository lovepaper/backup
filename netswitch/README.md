# NetSwitch · PC 网络控制开关

给测试工作用的一键网络开关，避免反复拔网线 / 关 WiFi。三种模式互不干扰。

- `netswitch.py` — 主程序源码（Python 3.11 + tkinter）
- `NetSwitch.exe` — 打包好的可执行文件，免 Python 环境，直接以管理员运行
- `使用说明.md` — 完整使用说明
- `upload_to_github.py` — 把本项目同步到 GitHub 的工具脚本

## 三种模式

| 模式 | 干什么 | 适用场景 |
|---|---|---|
| 整机断网 | 禁用/启用网卡，等价于拔网线 | 测整体离线表现 |
| 循环断网 | 断 X 秒 → 连 Y 秒 → 重复 N 轮 | 测断线重连、超时重试、状态机恢复 |
| 单应用断网 | 只掐某个 exe，其他软件照常上网 | 整机不下线，单独验证某个客户端 |

## 直接下载使用

`NetSwitch.exe` 已入库，下载后**右键 → 以管理员身份运行**（非管理员无法开关网卡）。
首次以管理员运行后点「创建免UAC快捷方式」，以后双击不再弹 UAC。

## 重新打包

需要**带 tkinter 的 Python 环境**（注：某些精简 Python 发行版不含 tkinter）：

```bat
pyinstaller --noconfirm --onefile --windowed --clean --name NetSwitch ^
  --hidden-import=keyboard --hidden-import=win32com.client netswitch.py
```

## 已知注意

- 防火墙规则只对新连接生效，已建立的长连接建议重启被测程序再验证
- 批量断网默认跳过 Hyper-V / WSL / Docker / VMware 等虚拟网卡
- 断网后远程桌面会断开，远程机器建议用「单应用断网」模式
