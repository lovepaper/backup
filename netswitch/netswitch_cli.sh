#!/bin/bash
# ==============================================================================
# NetSwitch CLI · macOS 零依赖版
# 纯 bash + macOS 自带命令（networksetup / pfctl / dnctl），无 Python / tkinter，
# 不用安装任何东西。功能与 GUI 版（netswitch.py）一致：
#   整机断网/恢复 · 弱网(限速/延迟/丢包) · 循环断网(模拟抖动)
#
# 用法:  sudo bash netswitch_cli.sh
# ==============================================================================

STATE="/var/tmp/netswitch_disabled.list"   # 断网时禁用的服务列表（恢复用）
PF_CONF="/tmp/netswitch_pf.conf"
ANCHOR="com.apple.netswitch"
PING_HOST="223.5.5.5"
# 与 GUI 版 MAC_VIRTUAL_KEYWORDS 一致（小写匹配）
VIRTUAL="bridge vpn virtual utun bluetooth iphone pan ppp tap tun thunderbolt usb"

# ---------- 基础 ----------

is_virtual() {
    local n
    n=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    local k
    for k in $VIRTUAL; do
        case "$n" in *"$k"*) return 0 ;; esac
    done
    return 1
}

list_services() {
    # 跳过表头行，去掉禁用标记的星号，去空行
    networksetup -listallnetworkservices 2>/dev/null \
        | tail -n +2 | sed 's/^\*//' | sed '/^[[:space:]]*$/d'
}

service_enabled() {
    [ "$(networksetup -getnetworkserviceenabled "$1" 2>/dev/null)" = "Enabled" ]
}

physical_services() {
    local s
    while IFS= read -r s; do
        [ -z "$s" ] && continue
        is_virtual "$s" && continue
        printf '%s\n' "$s"
    done <<< "$(list_services)"
}

# 静默断开所有物理网卡，记录到 $STATE
kill_all() {
    local s
    : > "$STATE"
    while IFS= read -r s; do
        if service_enabled "$s"; then
            networksetup -setnetworkserviceenabled "$s" off >/dev/null 2>&1 \
                && printf '%s\n' "$s" >> "$STATE"
        fi
    done <<< "$(physical_services)"
}

# 静默恢复（循环断网/Ctrl-C 用）
restore_silent() {
    local s
    [ -f "$STATE" ] || return 0
    while IFS= read -r s; do
        [ -z "$s" ] && continue
        networksetup -setnetworkserviceenabled "$s" on >/dev/null 2>&1
    done < "$STATE"
    rm -f "$STATE"
}

# ---------- 功能 ----------

do_status() {
    echo "== 网络服务 =="
    local s tag
    while IFS= read -r s; do
        [ -z "$s" ] && continue
        if is_virtual "$s"; then
            tag="(虚拟,跳过)"
        elif service_enabled "$s"; then
            tag="启用"
        else
            tag="已禁用"
        fi
        printf '  %-34s %s\n' "$s" "$tag"
    done <<< "$(list_services)"
    echo
    echo "== 连通性 =="
    if ping -c 1 -t 2 "$PING_HOST" >/dev/null 2>&1; then
        echo "  [OK] 网络连通 ($PING_HOST)"
    else
        echo "  [X]  网络不通 ($PING_HOST)"
    fi
    echo
    echo "== 弱网 =="
    local rules
    rules=$(pfctl -a "$ANCHOR" -s rules 2>/dev/null)
    if [ -n "$rules" ]; then
        echo "  弱网生效中:"
        printf '%s\n' "$rules" | sed 's/^/    /'
        echo "  管道配置:"
        dnctl -q show 2>/dev/null | sed 's/^/    /'
    else
        echo "  未开启"
    fi
}

do_kill() {
    local s n=0
    kill_all
    if [ -s "$STATE" ]; then
        while IFS= read -r s; do
            [ -z "$s" ] && continue
            echo "  [X] 已断开: $s"
            n=$((n + 1))
        done < "$STATE"
        echo "整机断网完成（共 $n 个服务）。恢复请选菜单 3。"
    else
        echo "没有可断开的物理网卡（可能已全部断开）。"
    fi
}

do_restore() {
    if [ ! -s "$STATE" ]; then
        echo "没有断网记录（当前无被本工具断开的服务）。"
        return
    fi
    local s
    while IFS= read -r s; do
        [ -z "$s" ] && continue
        networksetup -setnetworkserviceenabled "$s" on >/dev/null 2>&1 \
            && echo "  [OK] 已恢复: $s"
    done < "$STATE"
    rm -f "$STATE"
    echo "网络已恢复。"
}

cfg_pipe() {  # $1=管道号 $2=带宽Kbit $3=延迟ms $4=丢包%
    local args="pipe $1 config bw ${2}Kbit"
    if [ "${3:-0}" -gt 0 ] 2>/dev/null; then
        args="$args delay $3"
    fi
    if [ "${4:-0}" -gt 0 ] 2>/dev/null; then
        args="$args plr $(awk "BEGIN{printf \"%.4f\", $4/100}")"
    fi
    dnctl $args >/dev/null 2>&1
}

do_weak_on() {
    local dl ul lat loss
    printf '下行限速 Kbit/s  [回车=500]: '; read -r dl; dl=${dl:-500}
    printf '上行限速 Kbit/s  [回车=500]: '; read -r ul; ul=${ul:-500}
    printf '延迟 ms          [回车=100]: '; read -r lat; lat=${lat:-100}
    printf '丢包 %%           [回车=10] : '; read -r loss; loss=${loss:-10}
    case "$dl$ul$lat$loss" in *[!0-9]*) echo "参数必须是数字"; return ;; esac

    pfctl -e >/dev/null 2>&1     # 启用 pf（已启用会提示，忽略）
    cfg_pipe 1 "$ul" "$lat" "$loss"   # pipe1 = 出站(上行)
    cfg_pipe 2 "$dl" "$lat" "$loss"   # pipe2 = 入站(下行)

    printf 'dummynet out proto ip from any to any pipe 1\ndummynet in proto ip from any to any pipe 2\n' > "$PF_CONF"
    if pfctl -a "$ANCHOR" -f "$PF_CONF" >/dev/null 2>&1; then
        echo "弱网已开启: 下行 ${dl}Kbit/s · 上行 ${ul}Kbit/s · 延迟 ${lat}ms · 丢包 ${loss}%"
        echo "（系统级，对所有流量生效；关闭请选菜单 5）"
    else
        echo "加载 pf 规则失败（需要 root，请确认用 sudo 运行）"
    fi
}

do_weak_off() {
    pfctl -a "$ANCHOR" -F all >/dev/null 2>&1
    dnctl -q flush >/dev/null 2>&1
    echo "弱网已关闭，网络恢复原速。"
}

do_loop() {
    local off on
    printf '断网持续秒数 [回车=5]: '; read -r off; off=${off:-5}
    printf '恢复持续秒数 [回车=5]: '; read -r on;  on=${on:-5}
    case "$off$on" in *[!0-9]*) echo "参数必须是数字"; return ;; esac
    echo "循环断网中: 断 ${off}s / 通 ${on}s。按 Ctrl+C 停止并自动恢复网络。"
    trap 'restore_silent; echo; echo "已停止并恢复网络"; return' INT TERM
    while :; do
        kill_all
        echo "[$(date +%H:%M:%S)] [X] 断网 ${off}s ..."
        sleep "$off"
        restore_silent
        echo "[$(date +%H:%M:%S)] [OK] 恢复 ${on}s ..."
        sleep "$on"
    done
}

do_cleanup() {
    restore_silent
    do_weak_off
    echo "已恢复网络并清理弱网规则。"
}

# ---------- 入口 ----------

if [ "$EUID" -ne 0 ]; then
    echo "需要 root 权限（改网卡/限速都要）。请这样运行："
    echo "    sudo bash $0"
    exit 1
fi

trap 'echo; do_weak_off >/dev/null 2>&1; restore_silent; echo "已退出并恢复网络"; exit 0' INT TERM

while :; do
    echo
    echo "=========================================="
    echo "  NetSwitch CLI  (macOS 零依赖版)"
    echo "=========================================="
    echo "  1) 查看状态（网卡/连通性/弱网）"
    echo "  2) 整机断网（禁用所有物理网卡）"
    echo "  3) 恢复网络"
    echo "  4) 开启弱网（限速/延迟/丢包）"
    echo "  5) 关闭弱网"
    echo "  6) 循环断网（断 N 秒/通 N 秒，模拟抖动）"
    echo "  7) 一键还原（恢复网卡 + 关弱网）"
    echo "  0) 退出"
    echo "------------------------------------------"
    printf '选择: '
    read -r choice
    case "$choice" in
        1) do_status ;;
        2) do_kill ;;
        3) do_restore ;;
        4) do_weak_on ;;
        5) do_weak_off ;;
        6) do_loop ;;
        7) do_cleanup ;;
        0) do_weak_off >/dev/null 2>&1; restore_silent; echo "bye"; exit 0 ;;
        *) echo "无效选择" ;;
    esac
done
