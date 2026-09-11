# NetSwitch · PC 网络控制开关

给测试工作用的一键网络开关，避免反复拔网线 / 关 WiFi。三种模式互不干扰。

- `netswitch.py` — 主程序源码（Python 3.11 + tkinter）
- `使用说明.md` — 完整使用说明
- `upload_to_github.py` — 把源码同步到本仓库的工具脚本

## 三种模式

| 模式 | 干什么 | 适用场景 |
|---|---|---|
| 整机断网 | 禁用/启用网卡，等价于拔网线 | 测整体离线表现 |
| 循环断网 | 断 X 秒 → 连 Y 秒 → 重复 N 轮 | 测断线重连、超时重试、状态机恢复 |
| 单应用断网 | 只掐某个 exe，其他软件照常上网 | 整机不下线，单独验证某个客户端 |

## 重新打包

需要**带 tkinter 的 Python 环境**（注：某些精简 Python 发行版不含 tkinter）：

```bat
pyinstaller --noconfirm --onefile --windowed --clean --name NetSwitch ^
  --hidden-import=keyboard --hidden-import=win32com.client netswitch.py
```

打包产物 `NetSwitch.exe` 与运行配置 `netswitch_config.json` 不入库（见 .gitignore）。

## 已知注意

- 防火墙规则只对新连接生效，已建立的长连接建议重启被测程序再验证
- 批量断网默认跳过 Hyper-V / WSL / Docker / VMware 等虚拟网卡
- 断网后远程桌面会断开，远程机器建议用「单应用断网」模式
