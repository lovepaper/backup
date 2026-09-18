# NetSwitch macOS 命令速查（bash 零依赖版）

> 对应脚本：`netswitch_cli.sh`
> 适用场景：Mac 上装不了 Python / tkinter，或只想快速断网、限速、模拟弱网。
> 全部只用 macOS 自带命令（`networksetup` / `pfctl` / `dnctl` / `ping`），**不需要安装任何东西**。

---

## 0. 一条命令跑起来

```bash
curl -sL "https://raw.githubusercontent.com/lovepaper/backup/main/netswitch/netswitch_cli.sh?v=$(date +%s)" -o /tmp/ns.sh && sudo bash /tmp/ns.sh
```

- `sudo` 必须有：改网卡、改 pf 都要 root。
- `?v=$(date +%s)` 是防 CDN 缓存的随机参数，**更新脚本后必须带**，否则拉到旧版。
- 想留着以后用：`cp /tmp/ns.sh ~/netswitch_cli.sh`

---

## 1. 菜单 → 底层命令对照

| 菜单 | 作用 | 底层执行的命令 |
|---|---|---|
| 1 | 查看状态 | `networksetup -listallnetworkservices`、`ping`、`pfctl -a ... -s rules` |
| 2 | 整机断网 | 对每个物理网卡 `networksetup -setnetworkserviceenabled "X" off` |
| 3 | 恢复网络 | `networksetup -setnetworkserviceenabled "X" on`（按记录还原） |
| 4 | 开启弱网 | `dnctl pipe ... config` + `pfctl -a com.apple.netswitch -f ...` |
| 5 | 关闭弱网 | `pfctl -a com.apple.netswitch -F all` + `dnctl -q flush` |
| 6 | 循环断网 | 交替 菜单2/菜单3，Ctrl-C 自动恢复 |
| 7 | 一键还原 | 菜单3 + 菜单5 |
| 8 | 诊断排障 | 逐个命令打印 uid、命令路径、真实报错、`dnctl` 计数器 |

断网时会自动**跳过虚拟网卡**（bridge / vpn / virtual / utun / bluetooth / iphone / pan / ppp / tap / tun / thunderbolt / usb），避免把 VPN、虚拟机、Docker 网络搞挂。

---

## 2. 手动版（不想开脚本，直接敲终端）

### 网卡相关

```bash
# 列出所有网络服务（带 * 前缀的是已禁用）
networksetup -listallnetworkservices

# 查看某个服务是否启用
networksetup -getnetworkserviceenabled "Wi-Fi"

# 禁用 / 启用
sudo networksetup -setnetworkserviceenabled "Wi-Fi" off
sudo networksetup -setnetworkserviceenabled "Wi-Fi" on
```

### 弱网（限速 / 延迟 / 丢包）

```bash
# 1) 启用 pf
sudo pfctl -e

# 2) 配管道：pipe1=上行(出)，pipe2=下行(入)
sudo dnctl pipe 1 config bw 500Kbit delay 100 plr 0.1000
sudo dnctl pipe 2 config bw 500Kbit delay 100 plr 0.1000

# 3) 写规则并加载
printf 'dummynet out from any to any pipe 1\ndummynet in from any to any pipe 2\n' | sudo tee /tmp/netswitch_pf.conf
sudo pfctl -a com.apple.netswitch -f /tmp/netswitch_pf.conf
```

`plr` 是丢包率，取 0~1 的小数（10% → `0.1000`）。

### 关闭弱网 / 查看状态

```bash
# 关弱网
sudo pfctl -a com.apple.netswitch -F all
sudo dnctl -q flush

# 看当前生效规则
sudo pfctl -a com.apple.netswitch -s rules

# 看管道配置 + 流量计数
sudo dnctl show

# 看 pf 总状态
sudo pfctl -s info
```

---

## 3. 排障诊断块（脚本菜单 8 的内容，可单独粘贴跑）

```bash
sudo bash -c '
echo "== 权限 =="; echo "uid=$(id -u)  (\"0\" 表示 root)"; whoami
echo; echo "== 命令是否存在 =="
for c in pfctl dnctl networksetup ping awk; do printf "  %-14s %s\n" "$c" "$(command -v "$c" || echo 缺失!)"; done
echo; echo "== 逐个测试（带真实报错）=="
echo "-- pfctl -e --";        out=$(pfctl -e 2>&1);        echo "  rc=$? out=$out"
echo "-- dnctl pipe --";      out=$(dnctl pipe 1 config bw 500Kbit 2>&1); echo "  rc=$? out=$out"
echo "-- 写规则 --"
printf "dummynet out from any to any pipe 1\ndummynet in from any to any pipe 2\n" > /tmp/netswitch_pf.conf
out=$(pfctl -a com.apple.netswitch -f /tmp/netswitch_pf.conf 2>&1); echo "  rc=$? out=$out"
echo "-- 已加载规则 --"; pfctl -a com.apple.netswitch -s rules 2>&1 | sed "s/^/  /"
echo "-- 流量是否过管道（ping 后看计数器）--"; ping -c 3 -t 2 223.5.5.5 >/dev/null 2>&1; dnctl show 2>/dev/null | sed "s/^/  /"
echo "-- pf 状态 --"; pfctl -s info 2>&1 | head -5 | sed "s/^/  /"
'
```

**判读标准**：`uid=0` + `dnctl pipe` rc=0 + pf `Status: Enabled` → 弱网链路可用；
若规则加载 rc≠0，看 `out=` 里的真实报错（不要只看 rc）。

---

## 4. 本次实踩的坑（改规则前必读）

| 坑 | 现象 | 正确做法 |
|---|---|---|
| **`proto ip`** | `proto 0 cannot be used`，规则加载失败 | `ip` 在 `/etc/protocols` 是**协议号 0**，pf 拒绝。**省略 proto**，或写 `proto { tcp, udp }` / `all` |
| 锚点名 | 规则加载成功但不生效 | 必须用 **`com.apple.*` 前缀**：`/etc/pf.conf` 默认有 `dummynet-anchor "com.apple/*"`，子锚点才会被自动评估；自定义锚点不挂进主规则集就不会生效 |
| `pfctl -e` | 已启用时返回 **rc=1** | 输出是 `pf already enabled`，**不算失败**，别用 `$? -ne 0` 直接判死 |
| `No ALTQ support in kernel` | 看着像错误 | 老的 ALTQ 队列机制，跟 dummynet 无关，**忽略** |
| `Use of -f option, could result in flushing...` | 看着像错误 | 警告，非错误，**忽略** |
| 弱网"看起来开了" | 规则加载 rc=0 但没效果 | ping 一次后看 `dnctl show` 的**包计数器**是否增长——加载成功 ≠ 流量过了管道 |

---

## 5. 收尾 / 清理

```bash
# 一键还原（恢复网卡 + 关弱网）
sudo pfctl -a com.apple.netswitch -F all
sudo dnctl -q flush
sudo networksetup -setnetworkserviceenabled "Wi-Fi" on

# 删掉临时文件
rm -f /tmp/ns.sh /tmp/netswitch_pf.conf /var/tmp/netswitch_disabled.list
```

> ⚠️ 脚本的循环断网（菜单 6）用 **Ctrl-C** 停止会自动恢复网络；**不要直接关终端窗口**，
> 否则机器可能停在断网状态。真停在那了就跑上面那组还原命令。

---

## 6. 已知限制

- **macOS 不支持「单应用断网」**：没有按进程断网的系统接口（Windows 的防火墙规则那套在 Mac 上没有对应物）。Mac 上只有**整机断网**和**弱网**两种模式。
- 弱网是**系统级**的，对所有流量生效，不能只针对某个 App。
- 公司管控机器若 pf 被策略禁用，弱网不可用，但**整机断网照常可用**（走 `networksetup`，不依赖 pf）。
