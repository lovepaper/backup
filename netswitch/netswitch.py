# -*- coding: utf-8 -*-
"""
NetSwitch - 跨平台网络控制开关
面向测试工作：
  模式一 · 整机断网（禁用 / 启用网卡）
  模式二 · 循环断网（模拟网络抖动 / 反复重连）
  模式三 · 只掐单个应用的网络（仅 Windows；macOS 系统限制不支持）
  模式四 · 弱网模拟（系统级：带宽 / 延迟 / 丢包 / 抖动 / 乱序）

Windows：网卡用 netsh / PowerShell，应用阻断用 Windows 防火墙，弱网用 WinDivert。
macOS ：网卡用 networksetup，弱网用 pfctl + dnctl（dummynet）。

所有开关操作均需管理员 / root 权限。
"""

import os
import sys
import json
import time
import ctypes
import random
import hashlib
import threading
import subprocess
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME = "NetSwitch"
APP_VERSION = "2.0.0"
RULE_PREFIX = "NetSwitch_Block_"
TASK_NAME = "NetSwitch_Admin_Launcher"
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

_BASE = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_BASE, "netswitch_config.json")

FONT = ("Helvetica", 10) if IS_MAC else ("Microsoft YaHei UI", 10)

# 虚拟机/隧道类网卡（Windows）
WIN_VIRTUAL_KEYWORDS = (
    "hyper-v", "vethernet", "vmware", "virtualbox", "vbox", "wsl",
    "tap-windows", "tap-win32", "zerotier", "tailscale", "openvpn",
    "virtual", "loopback", "npf_", "npcap", "docker", "bluetooth", "miniport",
)
# macOS 虚拟/非物理服务
MAC_VIRTUAL_KEYWORDS = (
    "bridge", "vpn", "virtual", "utun", "bluetooth", "iphone", "pan",
    "ppp", "tap", "tun", "thunderbolt bridge", "usb tether",
)

# 全局热键依赖 keyboard 库（仅 Windows 有意义），缺失时自动降级
try:
    import keyboard  # type: ignore
    HAS_KEYBOARD = True and IS_WIN
except Exception:
    HAS_KEYBOARD = False


# --------------------------------------------------------------------------
# 跨平台基础工具
# --------------------------------------------------------------------------
def run_cmd(cmd, shell=None, timeout=25):
    """执行命令，返回 (ok, text)。cmd 为 str 时走 shell，list 时直接 exec。"""
    if shell is None:
        shell = isinstance(cmd, str)
    try:
        kwargs = dict(capture_output=True, timeout=timeout)
        if not shell and IS_WIN:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run(cmd, shell=shell, **kwargs)
        out = (p.stdout or b"") + (p.stderr or b"")
        text = ""
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                text = out.decode(enc)
                break
            except Exception:
                continue
        if not text:
            text = out.decode("utf-8", errors="replace")
        return p.returncode == 0, text.strip()
    except subprocess.TimeoutExpired:
        return False, "命令执行超时"
    except Exception as e:
        return False, str(e)


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Windows 实现
# --------------------------------------------------------------------------
def win_ps_json(script, timeout=25):
    ok, text = run_cmd([
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script
    ], shell=False, timeout=timeout)
    if not ok or not text:
        return None
    start = None
    for i, ch in enumerate(text):
        if ch in "[{":
            start = i
            break
    if start is None:
        return None
    snippet = text[start:]
    try:
        return json.loads(snippet)
    except Exception:
        try:
            obj, _ = json.JSONDecoder().raw_decode(snippet)
            return obj
        except Exception:
            return None


def win_list_adapters():
    data = win_ps_json(
        "Get-NetAdapter -ErrorAction SilentlyContinue | "
        "Select-Object Name,InterfaceDescription,Status | ConvertTo-Json -Compress"
    )
    res = []
    if not data:
        return res
    if isinstance(data, dict):
        data = [data]
    for it in data:
        res.append({
            "name": it.get("Name", ""),
            "desc": it.get("InterfaceDescription", ""),
            "status": it.get("Status", "") or "",
        })
    return res


def win_is_virtual(ad):
    blob = ("%s %s" % (ad.get("name", ""), ad.get("desc", ""))).lower()
    return any(k in blob for k in WIN_VIRTUAL_KEYWORDS)


def win_status_kind(ad):
    s = str(ad.get("status", "")).strip().lower()
    if s.startswith("disabled"):
        return "disabled"
    if s.startswith("not present"):
        return "absent"
    if s.startswith("up"):
        return "up"
    return "disconnected"


def win_set_adapter(name, enable):
    verb = "enable" if enable else "disable"
    ok, out = run_cmd('netsh interface set interface name="%s" admin=%s' % (name, verb), timeout=30)
    if not ok:
        verb2 = "Enable-NetAdapter" if enable else "Disable-NetAdapter"
        ok, out2 = run_cmd(
            'powershell -NoProfile -ExecutionPolicy Bypass -Command '
            '%s -Name "%s" -Confirm:$false -ErrorAction Stop' % (verb2, name))
        return ok, out + " || " + out2
    return ok, out


def win_ping_once(host="223.5.5.5", timeout_ms=1500):
    ok, out = run_cmd("ping -n 1 -w %d %s" % (timeout_ms, host), timeout=12)
    return ("TTL=" in out.upper()) or ("time=" in out.lower())


def win_rule_name_for(exe_path):
    h = hashlib.md5(exe_path.lower().encode("utf-8")).hexdigest()[:8]
    return RULE_PREFIX + h


def win_missing(out):
    low = (out or "").lower()
    return ("no rules match" in low) or ("没有匹配的规则" in out) or ("找不到" in out)


def win_app_blocked(exe_path):
    ok, out = run_cmd('netsh advfirewall firewall show rule name="%s"' % win_rule_name_for(exe_path))
    return bool(ok) and not win_missing(out)


def win_block_app(exe_path):
    rn = win_rule_name_for(exe_path)
    detail = []
    for direction in ("out", "in"):
        ok, out = run_cmd(
            'netsh advfirewall firewall add rule name="%s" dir=%s action=block '
            'program="%s" enable=yes profile=any' % (rn, direction, exe_path))
        detail.append((direction, ok, out))
    return all(d[1] for d in detail), detail


def win_unblock_app(exe_path):
    ok, out = run_cmd('netsh advfirewall firewall delete rule name="%s"' % win_rule_name_for(exe_path))
    if not ok and win_missing(out):
        return True, "规则本就不存在"
    return ok, out


def win_list_rules():
    data = win_ps_json(
        "Get-NetFirewallRule -ErrorAction SilentlyContinue | "
        "Where-Object {$_.DisplayName -like '%s*'} | "
        "ForEach-Object { "
        "  $p = ($_ | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue).Program; "
        "  [PSCustomObject]@{ Name=$_.DisplayName; Direction=$_.Direction; Program=$p } "
        "} | ConvertTo-Json -Compress" % RULE_PREFIX
    )
    if not data:
        return []
    if isinstance(data, dict):
        data = [data]
    return [{"name": d.get("Name", ""), "direction": d.get("Direction", ""),
             "program": d.get("Program", "")} for d in data]


def win_delete_rule(name):
    return run_cmd('netsh advfirewall firewall delete rule name="%s"' % name)


def win_list_processes():
    data = win_ps_json(
        "Get-Process -ErrorAction SilentlyContinue | "
        "Where-Object {$_.Path -ne $null -and $_.Path -like '*.exe'} | "
        "Select-Object -ExpandProperty Path -Unique | Sort-Object | ConvertTo-Json -Compress",
        timeout=40,
    )
    paths = []
    if data:
        paths = data if isinstance(data, list) else [data]
    res = []
    seen = set()
    for p in paths:
        if p and os.path.exists(p) and p.lower() not in seen:
            seen.add(p.lower())
            res.append((os.path.basename(p), p))
    res.sort(key=lambda x: x[0].lower())
    return res


# --------------------------------------------------------------------------
# macOS 实现
# --------------------------------------------------------------------------
def mac_is_admin():
    return os.geteuid() == 0


def mac_run_priv(cmd):
    """需要特权的命令：已是 root 直接跑，否则加 sudo -n（非交互）"""
    if mac_is_admin():
        return run_cmd(cmd, shell=False, timeout=30)
    return run_cmd(["sudo", "-n"] + list(cmd), shell=False, timeout=30)


def mac_list_adapters():
    ok, out = run_cmd(["networksetup", "-listallnetworkservices"], shell=False, timeout=20)
    res = []
    if not ok:
        return res
    started = False
    for ln in out.splitlines():
        s = ln.strip()
        if not started:
            if "denotes" in s.lower() or s.startswith("An asterisk"):
                started = True
            continue
        if not s:
            continue
        name = s.replace("*", "").strip()
        if not name:
            continue
        enabled = mac_service_enabled(name)
        status = "up" if enabled else "disabled"
        res.append({"name": name, "desc": "", "status": status})
    return res


def mac_service_enabled(name):
    ok, out = run_cmd(["networksetup", "-getnetworkserviceenabled", name], shell=False, timeout=15)
    return ok and "enabled" in out.lower()


def mac_is_virtual(ad):
    n = ad.get("name", "").lower()
    return any(k in n for k in MAC_VIRTUAL_KEYWORDS)


def mac_status_kind(ad):
    s = str(ad.get("status", "")).strip().lower()
    if s == "disabled":
        return "disabled"
    return "up"


def mac_set_adapter(name, enable):
    verb = "on" if enable else "off"
    return mac_run_priv(["networksetup", "-setnetworkserviceenabled", name, verb])


def mac_ping_once(host="223.5.5.5", timeout_ms=1500):
    ok, out = run_cmd(["ping", "-c", "1", "-W", str(timeout_ms), host], shell=False, timeout=12)
    low = out.lower()
    return ("time=" in low) or ("1 packets received" in low) or ("1 received" in low)


def mac_list_processes():
    ok, out = run_cmd(["ps", "-ax", "-o", "pid=,command="], shell=False, timeout=30)
    res = []
    seen = set()
    if ok:
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            parts = ln.split(None, 1)
            if len(parts) < 2:
                continue
            path = parts[1].strip()
            if path.lower().endswith(".app"):
                continue
            if path and os.path.exists(path) and path.lower() not in seen:
                seen.add(path.lower())
                res.append((os.path.basename(path), path))
    res.sort(key=lambda x: x[0].lower())
    return res


def _mac_dnctl_cfg(pipe_no, kbps, latency_ms, loss_pct):
    if kbps and kbps > 0:
        args = ["dnctl", "pipe", str(pipe_no), "config", "bw", "%dKbit" % int(kbps)]
    else:
        args = ["dnctl", "pipe", str(pipe_no), "config", "bw", "1000000Kbit"]
    if latency_ms and latency_ms > 0:
        args += ["delay", str(int(latency_ms))]
    if loss_pct and loss_pct > 0:
        args += ["plr", "%.4f" % (loss_pct / 100.0)]
    return args


def mac_set_weak(profile):
    # 1) 启用 pf
    mac_run_priv(["pfctl", "-e"])
    dl = profile.get("dl_kbps") or 0
    ul = profile.get("ul_kbps") or 0
    lat = profile.get("latency_ms") or 0
    loss = profile.get("loss_pct") or 0
    # 2) 配置两条 dummynet 管道：pipe1 出站(上行)，pipe2 入站(下行)
    r1 = mac_run_priv(_mac_dnctl_cfg(1, ul, lat, loss))
    r2 = mac_run_priv(_mac_dnctl_cfg(2, dl, lat, loss))
    # 3) 写 pf 规则到临时文件并加载到 com.apple.netswitch 锚点
    rules = "dummynet out proto ip from any to any pipe 1\ndummynet in proto ip from any to any pipe 2\n"
    tmp = "/tmp/netswitch_pf.conf"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(rules)
    except Exception as e:
        return False, "写 pf 规则失败：%s" % e
    ok, out = mac_run_priv(["pfctl", "-a", "com.apple.netswitch", "-f", tmp])
    if not ok:
        return False, "加载 pf 规则失败：%s" % (out or "")[:300]
    return True, "弱网已开启（系统级 · 带宽/延迟/丢包）"


def mac_clear_weak():
    mac_run_priv(["pfctl", "-a", "com.apple.netswitch", "-F", "all"])
    mac_run_priv(["dnctl", "-q", "flush"])
    return True, "弱网已停止，网络已恢复"


def mac_weak_active():
    ok, out = run_cmd(["pfctl", "-a", "com.apple.netswitch", "-s", "rules"], shell=False, timeout=15)
    return ok and bool(out.strip())


# --------------------------------------------------------------------------
# 平台抽象
# --------------------------------------------------------------------------
class WinPlatform:
    name = "win"

    def is_admin(self):
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def elevate(self):
        try:
            if getattr(sys, "frozen", False):
                exe, arg = sys.executable, ""
            else:
                exe, arg = sys.executable, '"%s"' % os.path.abspath(__file__)
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, arg, os.getcwd(), 1)
            return rc > 32
        except Exception:
            return False

    def list_adapters(self):
        return win_list_adapters()

    def is_virtual(self, ad):
        return win_is_virtual(ad)

    def status_kind(self, ad):
        return win_status_kind(ad)

    def set_adapter(self, name, enable):
        return win_set_adapter(name, enable)

    def ping_once(self, host="223.5.5.5"):
        return win_ping_once(host)

    def app_blocked(self, path):
        return win_app_blocked(path)

    def block_app(self, path):
        return win_block_app(path)

    def unblock_app(self, path):
        return win_unblock_app(path)

    def list_rules(self):
        return win_list_rules()

    def delete_rule(self, name):
        return win_delete_rule(name)

    def list_processes(self):
        return win_list_processes()

    def weak_supported(self):
        return True

    def has_jitter_reorder(self):
        return True

    def app_block_supported(self):
        return True

    def set_weak(self, profile):
        try:
            import windivert_throttle as wd  # 懒加载，失败不影响其它功能
        except Exception as e:
            return False, "弱网引擎不可用：%s" % e
        return wd.start_weak(profile)

    def clear_weak(self):
        try:
            import windivert_throttle as wd
        except Exception:
            return True, "没有运行中的弱网"
        return wd.stop_weak()

    def weak_active(self):
        try:
            import windivert_throttle as wd
            return wd.is_active()
        except Exception:
            return False

    def weak_stats(self):
        try:
            import windivert_throttle as wd
            return wd.get_stats()
        except Exception:
            return {"dropped": 0, "sent": 0, "active": False}


class MacPlatform:
    name = "mac"

    def is_admin(self):
        return mac_is_admin()

    def elevate(self):
        try:
            if getattr(sys, "frozen", False):
                target = '"%s"' % sys.executable
                args = ""
            else:
                target = '"%s"' % sys.executable
                args = '"%s"' % os.path.abspath(__file__)
            script = 'do shell script "%s %s" with administrator privileges' % (target, args)
            rc = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=30)
            return rc.returncode == 0
        except Exception:
            return False

    def list_adapters(self):
        return mac_list_adapters()

    def is_virtual(self, ad):
        return mac_is_virtual(ad)

    def status_kind(self, ad):
        return mac_status_kind(ad)

    def set_adapter(self, name, enable):
        return mac_set_adapter(name, enable)

    def ping_once(self, host="223.5.5.5"):
        return mac_ping_once(host)

    def app_blocked(self, path):
        return False

    def block_app(self, path):
        return False, "macOS 不支持按进程精准断网（系统限制），请用弱网或整机断网模式"

    def unblock_app(self, path):
        return True, "macOS 无需放行"

    def list_rules(self):
        return []

    def delete_rule(self, name):
        return True, ""

    def list_processes(self):
        return mac_list_processes()

    def weak_supported(self):
        return True

    def has_jitter_reorder(self):
        return False  # macOS dummynet 不支持抖动/乱序

    def app_block_supported(self):
        return False

    def set_weak(self, profile):
        if not self.is_admin():
            return False, "需要 root 权限，请点「以 root 重启」"
        return mac_set_weak(profile)

    def clear_weak(self):
        if not self.is_admin():
            return True, "没有运行中的弱网"
        return mac_clear_weak()

    def weak_active(self):
        return mac_weak_active()

    def weak_stats(self):
        return {"dropped": 0, "sent": 0, "active": self.weak_active()}


def get_platform():
    return MacPlatform() if IS_MAC else WinPlatform()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class NetSwitchApp:
    def __init__(self, root):
        self.root = root
        self.platform = get_platform()
        self.root.title("%s v%s - 网络控制开关" % (APP_NAME, APP_VERSION))
        self.root.geometry("800x760" if IS_MAC else "780x700")
        self.root.minsize(720, 680)

        self.cfg = load_config()
        self.adapters = []
        self.rules = []
        self.row_map = {}
        self._stop_event = threading.Event()
        self._worker = None
        self._weak_active = False

        self.BG, self.CARD = "#f4f5f7", "#ffffff"
        self.FG, self.MUTED = "#1f2328", "#6b7280"
        self.GREEN, self.RED, self.ORANGE, self.ACCENT = "#16a34a", "#dc2626", "#ea580c", "#2563eb"

        self.root.configure(bg=self.BG)
        self._build_style()
        self._build_ui()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_all()
        if HAS_KEYBOARD:
            self._bind_hotkeys()

    def _build_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=self.BG, foreground=self.FG, fieldbackground="#ffffff", font=FONT)
        st.configure("TFrame", background=self.BG)
        st.configure("Card.TFrame", background=self.CARD)
        st.configure("TLabel", background=self.BG, foreground=self.FG, font=FONT)
        st.configure("Card.TLabel", background=self.CARD, foreground=self.FG, font=FONT)
        st.configure("Title.TLabel", background=self.CARD, foreground=self.FG,
                     font=(FONT[0], 12, "bold"))
        st.configure("Muted.TLabel", background=self.CARD, foreground=self.MUTED, font=(FONT[0], 9))
        st.configure("TButton", font=FONT, padding=6)
        st.configure("TCheckbutton", background=self.CARD, foreground=self.FG, font=FONT)
        st.configure("TCombobox", font=FONT)
        st.configure("TEntry", font=FONT)
        st.configure("TSpinbox", font=FONT)

    def _card(self):
        return ttk.Frame(self.root, style="Card.TFrame", padding=14)

    def _build_ui(self):
        top = self._card()
        top.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(top, text="%s   %s 网络控制开关" % (APP_NAME, "macOS" if IS_MAC else "PC"),
                  style="Title.TLabel").pack(side="left")
        self.admin_var = tk.StringVar(value="")
        self.admin_lbl = ttk.Label(top, textvariable=self.admin_var, style="Muted.TLabel")
        self.admin_lbl.pack(side="right")
        ttk.Label(top, text="v%s" % APP_VERSION, style="Muted.TLabel").pack(side="right", padx=(0, 12))

        self._build_net_card()
        self._build_loop_card()
        if self.platform.app_block_supported():
            self._build_app_card()
        else:
            self._build_app_note()
        self._build_weak_card()
        self._build_log_card()

        bot = ttk.Frame(self.root, padding=(12, 4))
        bot.pack(fill="x")
        tip = ("热键：Ctrl+Alt+K 断网 / Ctrl+Alt+R 恢复 / Ctrl+Alt+S 停止任务" if HAS_KEYBOARD
               else "（未检测到 keyboard 库，全局热键不可用）")
        ttk.Label(bot, text=tip, style="Muted.TLabel").pack(side="left")
        if IS_WIN:
            self.btn_task = ttk.Button(bot, text="创建免UAC快捷方式", command=self.create_uac_free)
            self.btn_task.pack(side="right")
        self.btn_elevate = ttk.Button(bot, text="以管理员重启" if IS_WIN else "以 root 重启",
                                      command=self.do_elevate)
        self.btn_elevate.pack(side="right", padx=(0, 8))

    def _build_net_card(self):
        c = self._card()
        c.pack(fill="x", padx=12, pady=6)
        ttk.Label(c, text="模式一 · 整机断网（禁用 / 启用网卡）", style="Title.TLabel").pack(anchor="w")

        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x", pady=(10, 6))
        ttk.Label(row, text="网卡/服务：", style="Card.TLabel").pack(side="left")
        self.adapter_var = tk.StringVar()
        self.adapter_cb = ttk.Combobox(row, textvariable=self.adapter_var, state="readonly", width=40)
        self.adapter_cb.pack(side="left", padx=(4, 10))
        self.adapter_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_net_status())
        ttk.Button(row, text="刷新", command=self.refresh_all, width=8).pack(side="left")
        if IS_WIN:
            self.phys_only_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(row, text="批量操作时跳过虚拟网卡", variable=self.phys_only_var,
                            style="TCheckbutton").pack(side="left", padx=(14, 0))

        st = ttk.Frame(c, style="Card.TFrame")
        st.pack(fill="x", pady=(0, 8))
        self.net_dot = tk.Canvas(st, width=14, height=14, bg=self.CARD, highlightthickness=0)
        self.net_dot.pack(side="left")
        self.net_dot.create_oval(2, 2, 12, 12, fill=self.MUTED, outline="")
        self.net_state_var = tk.StringVar(value="检测中…")
        ttk.Label(st, textvariable=self.net_state_var, style="Card.TLabel").pack(side="left", padx=(6, 0))
        self.ping_var = tk.StringVar(value="")
        ttk.Label(st, textvariable=self.ping_var, style="Muted.TLabel").pack(side="left", padx=(16, 0))

        bro = ttk.Frame(c, style="Card.TFrame")
        bro.pack(fill="x", pady=(2, 0))
        self.btn_off = ttk.Button(bro, text="断开网络", command=lambda: self.net_toggle(False), width=14)
        self.btn_off.pack(side="left")
        self.btn_on = ttk.Button(bro, text="恢复网络", command=lambda: self.net_toggle(True), width=14)
        self.btn_on.pack(side="left", padx=(10, 0))
        if IS_WIN:
            self.btn_off_all = ttk.Button(bro, text="断开全部网卡", command=lambda: self.net_toggle_all(False), width=14)
            self.btn_off_all.pack(side="left", padx=(10, 0))
            self.btn_on_all = ttk.Button(bro, text="恢复全部网卡", command=lambda: self.net_toggle_all(True), width=14)
            self.btn_on_all.pack(side="left", padx=(10, 0))

        ar = ttk.Frame(c, style="Card.TFrame")
        ar.pack(fill="x", pady=(8, 0))
        self.auto_var = tk.BooleanVar(value=self.cfg.get("auto_restore", False))
        ttk.Checkbutton(ar, text="断网后自动恢复（防锁死）：", variable=self.auto_var,
                        command=self._persist_opts, style="TCheckbutton").pack(side="left")
        self.auto_sec_var = tk.StringVar(value=str(self.cfg.get("auto_sec", 30)))
        self.spin = ttk.Spinbox(ar, from_=5, to=3600, width=6, textvariable=self.auto_sec_var,
                                command=self._persist_opts)
        self.spin.pack(side="left", padx=(4, 0))
        ttk.Label(ar, text="秒", style="Card.TLabel").pack(side="left")
        self.auto_left_var = tk.StringVar(value="")
        ttk.Label(ar, textvariable=self.auto_left_var, style="Muted.TLabel").pack(side="left", padx=(14, 0))

    def _build_loop_card(self):
        c = self._card()
        c.pack(fill="x", padx=12, pady=6)
        ttk.Label(c, text="模式二 · 循环断网（模拟网络抖动 / 反复重连）", style="Title.TLabel").pack(anchor="w")

        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x", pady=(10, 4))
        self.loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="启用：断", variable=self.loop_var, style="TCheckbutton").pack(side="left")
        self.loop_off_var = tk.StringVar(value=str(self.cfg.get("loop_off", 10)))
        ttk.Spinbox(row, from_=1, to=3600, width=6, textvariable=self.loop_off_var,
                    command=self._persist_opts).pack(side="left", padx=(4, 0))
        ttk.Label(row, text="秒 → 连", style="Card.TLabel").pack(side="left", padx=(6, 0))
        self.loop_on_var = tk.StringVar(value=str(self.cfg.get("loop_on", 10)))
        ttk.Spinbox(row, from_=1, to=3600, width=6, textvariable=self.loop_on_var,
                    command=self._persist_opts).pack(side="left", padx=(4, 0))
        ttk.Label(row, text="秒，重复", style="Card.TLabel").pack(side="left", padx=(6, 0))
        self.loop_cnt_var = tk.StringVar(value=str(self.cfg.get("loop_cnt", 5)))
        ttk.Spinbox(row, from_=1, to=999, width=6, textvariable=self.loop_cnt_var,
                    command=self._persist_opts).pack(side="left", padx=(4, 0))
        ttk.Label(row, text="轮（0=不限）", style="Card.TLabel").pack(side="left", padx=(6, 0))

        bro = ttk.Frame(c, style="Card.TFrame")
        bro.pack(fill="x", pady=(6, 0))
        self.btn_loop_start = ttk.Button(bro, text="开始循环", command=self.loop_start, width=14)
        self.btn_loop_start.pack(side="left")
        self.btn_loop_stop = ttk.Button(bro, text="停止并恢复", command=self.loop_stop, width=14,
                                        state="disabled")
        self.btn_loop_stop.pack(side="left", padx=(10, 0))
        self.loop_state_var = tk.StringVar(value="")
        ttk.Label(bro, textvariable=self.loop_state_var, style="Muted.TLabel").pack(side="left", padx=(16, 0))

    def _build_app_card(self):
        c = self._card()
        c.pack(fill="x", padx=12, pady=6)
        ttk.Label(c, text="模式三 · 只掐单个应用的网络（其余软件照常上网）",
                  style="Title.TLabel").pack(anchor="w")

        prow = ttk.Frame(c, style="Card.TFrame")
        prow.pack(fill="x", pady=(10, 6))
        ttk.Label(prow, text="程序：", style="Card.TLabel").pack(side="left")
        self.exe_var = tk.StringVar()
        ttk.Entry(prow, textvariable=self.exe_var, width=52).pack(side="left", padx=(4, 8))
        ttk.Button(prow, text="浏览…", command=self.pick_exe, width=8).pack(side="left")
        ttk.Button(prow, text="从进程选", command=self.pick_process, width=10).pack(side="left", padx=(6, 0))

        ttk.Label(c, text="已被 NetSwitch 阻断的程序（双击可反填到输入框）：",
                  style="Card.TLabel").pack(anchor="w")
        self.block_list = tk.Listbox(self.root, height=4, font=("Consolas", 9),
                                     relief="solid", borderwidth=1, bg="#ffffff", fg=self.FG)
        self.block_list.pack(fill="x", padx=26, pady=(2, 6))
        self.block_list.bind("<Double-Button-1>", self.on_block_dbl)

        bro = ttk.Frame(c, style="Card.TFrame")
        bro.pack(fill="x")
        self.btn_block = ttk.Button(bro, text="阻断该程序", command=self.app_block, width=14)
        self.btn_block.pack(side="left")
        self.btn_unblock = ttk.Button(bro, text="放行该程序", command=self.app_unblock, width=14)
        self.btn_unblock.pack(side="left", padx=(10, 0))
        self.btn_clear_rules = ttk.Button(bro, text="清理全部阻断规则", command=self.clear_rules, width=18)
        self.btn_clear_rules.pack(side="left", padx=(10, 0))
        ttk.Label(c, text="提示：规则只对新连接生效，已建立的长连接建议重启被测程序。",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

    def _build_app_note(self):
        c = self._card()
        c.pack(fill="x", padx=12, pady=6)
        ttk.Label(c, text="模式三 · 单应用断网（本平台不可用）", style="Title.TLabel").pack(anchor="w")
        ttk.Label(c, text="macOS 系统限制：pf 防火墙无法按进程/端口精准阻断单个 App 的出站流量。\n"
                          "需要单独掐某个程序时，请用「模式四 · 弱网」对整个系统限速，或用「模式一」整机断网。",
                  style="Card.TLabel").pack(anchor="w", pady=(8, 0))

    def _build_weak_card(self):
        c = self._card()
        c.pack(fill="x", padx=12, pady=6)
        ttk.Label(c, text="模式四 · 弱网模拟（系统级：所有流量受影响）", style="Title.TLabel").pack(anchor="w")

        r1 = ttk.Frame(c, style="Card.TFrame")
        r1.pack(fill="x", pady=(10, 4))
        ttk.Label(r1, text="下行带宽(kbps)：", style="Card.TLabel").pack(side="left")
        self.dl_var = tk.StringVar(value=str(self.cfg.get("dl_kbps", 0)))
        ttk.Spinbox(r1, from_=0, to=1000000, width=10, textvariable=self.dl_var,
                    command=self._persist_weak).pack(side="left", padx=(4, 10))
        ttk.Label(r1, text="上行带宽(kbps)：", style="Card.TLabel").pack(side="left")
        self.ul_var = tk.StringVar(value=str(self.cfg.get("ul_kbps", 0)))
        ttk.Spinbox(r1, from_=0, to=1000000, width=10, textvariable=self.ul_var,
                    command=self._persist_weak).pack(side="left", padx=(4, 10))
        ttk.Label(r1, text="(0 = 不限速)", style="Muted.TLabel").pack(side="left")

        preset = ttk.Frame(c, style="Card.TFrame")
        preset.pack(fill="x", pady=(0, 4))
        ttk.Label(preset, text="快速档：", style="Card.TLabel").pack(side="left")
        for lbl, v in (("64K", 64), ("256K", 256), ("1M", 1000), ("10M", 10000), ("100M", 100000)):
            ttk.Button(preset, text=lbl, width=6,
                       command=lambda vv=v: self._apply_preset(vv)).pack(side="left", padx=(2, 0))

        r2 = ttk.Frame(c, style="Card.TFrame")
        r2.pack(fill="x", pady=(0, 4))
        ttk.Label(r2, text="延迟(ms)：", style="Card.TLabel").pack(side="left")
        self.lat_var = tk.StringVar(value=str(self.cfg.get("latency_ms", 0)))
        ttk.Spinbox(r2, from_=0, to=5000, width=8, textvariable=self.lat_var,
                    command=self._persist_weak).pack(side="left", padx=(4, 10))
        ttk.Label(r2, text="抖动(ms)：", style="Card.TLabel").pack(side="left")
        self.jit_var = tk.StringVar(value=str(self.cfg.get("jitter_ms", 0)))
        self.jit_spin = ttk.Spinbox(r2, from_=0, to=2000, width=8, textvariable=self.jit_var,
                                    command=self._persist_weak)
        self.jit_spin.pack(side="left", padx=(4, 10))
        ttk.Label(r2, text="丢包(%)：", style="Card.TLabel").pack(side="left")
        self.loss_var = tk.StringVar(value=str(self.cfg.get("loss_pct", 0)))
        ttk.Spinbox(r2, from_=0, to=100, width=8, textvariable=self.loss_var,
                    command=self._persist_weak).pack(side="left", padx=(4, 10))
        ttk.Label(r2, text="乱序(%)：", style="Card.TLabel").pack(side="left")
        self.reord_var = tk.StringVar(value=str(self.cfg.get("reorder_pct", 0)))
        self.reord_spin = ttk.Spinbox(r2, from_=0, to=100, width=8, textvariable=self.reord_var,
                                      command=self._persist_weak)
        self.reord_spin.pack(side="left", padx=(4, 10))
        if not self.platform.has_jitter_reorder():
            self.jit_spin.configure(state="disabled")
            self.reord_spin.configure(state="disabled")
            ttk.Label(r2, text="(macOS dummynet 不支持抖动/乱序)", style="Muted.TLabel").pack(side="left", padx=(8, 0))

        bro = ttk.Frame(c, style="Card.TFrame")
        bro.pack(fill="x", pady=(6, 0))
        self.btn_weak_on = ttk.Button(bro, text="开始弱网", command=self.weak_start, width=14)
        self.btn_weak_on.pack(side="left")
        self.btn_weak_off = ttk.Button(bro, text="停止弱网", command=self.weak_stop, width=14,
                                       state="disabled")
        self.btn_weak_off.pack(side="left", padx=(10, 0))
        self.weak_state_var = tk.StringVar(value="")
        ttk.Label(bro, textvariable=self.weak_state_var, style="Muted.TLabel").pack(side="left", padx=(16, 0))
        ttk.Label(c, text="弱网对所有进出流量生效，开启后建议重启被测程序再观察。停止后恢复原速。",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

    def _build_log_card(self):
        c = self._card()
        c.pack(fill="both", expand=True, padx=12, pady=6)
        ttk.Label(c, text="运行日志", style="Title.TLabel").pack(anchor="w")
        self.log = tk.Text(c, height=8, font=("Consolas", 9), relief="solid", borderwidth=1,
                           bg="#ffffff", fg=self.FG, wrap="word")
        self.log.pack(fill="both", expand=True, pady=(6, 0))
        self.log.configure(state="disabled")

    # ---------------- 通用 ----------------
    def log_line(self, msg, level="info"):
        def _p():
            self.log.configure(state="normal")
            self.log.insert("end", "[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))
            self.log.see("end")
            self.log.configure(state="disabled")
        if threading.current_thread() is threading.main_thread():
            _p()
        else:
            self.root.after(0, _p)

    def set_dot(self, color):
        def _p():
            self.net_dot.delete("all")
            self.net_dot.create_oval(2, 2, 12, 12, fill=color, outline="")
        if threading.current_thread() is threading.main_thread():
            _p()
        else:
            self.root.after(0, _p)

    def _busy(self, flag):
        def _p():
            state = "disabled" if flag else "normal"
            for b in (self.btn_off, self.btn_on):
                try:
                    b.configure(state=state)
                except Exception:
                    pass
            if IS_WIN:
                for b in (self.btn_off_all, self.btn_on_all):
                    try:
                        b.configure(state=state)
                    except Exception:
                        pass
        self.root.after(0, _p)

    def _persist_opts(self):
        try:
            self.cfg.update({
                "auto_restore": bool(self.auto_var.get()),
                "auto_sec": int(self.auto_sec_var.get() or 30),
                "loop_off": int(self.loop_off_var.get() or 10),
                "loop_on": int(self.loop_on_var.get() or 10),
                "loop_cnt": int(self.loop_cnt_var.get() or 5),
            })
            save_config(self.cfg)
        except Exception:
            pass

    def _persist_weak(self):
        try:
            self.cfg.update({
                "dl_kbps": int(self.dl_var.get() or 0),
                "ul_kbps": int(self.ul_var.get() or 0),
                "latency_ms": int(self.lat_var.get() or 0),
                "jitter_ms": int(self.jit_var.get() or 0),
                "loss_pct": int(self.loss_var.get() or 0),
                "reorder_pct": int(self.reord_var.get() or 0),
            })
            save_config(self.cfg)
        except Exception:
            pass

    def _apply_preset(self, v):
        self.dl_var.set(str(v))
        self.ul_var.set(str(v))
        self._persist_weak()

    def ensure_admin(self):
        if self.platform.is_admin():
            return True
        messagebox.showwarning(
            "需要权限",
            "开关网卡 / 改防火墙 / 弱网都需要管理员(root)权限。\n\n"
            "请点右下角「%s」后重试。" % ("以 root 重启" if IS_MAC else "以管理员重启"))
        return False

    # ---------------- 刷新 ----------------
    def refresh_all(self):
        self.refresh_admin()
        threading.Thread(target=lambda: self.root.after(
            0, lambda: self._apply_adapters(self.platform.list_adapters())), daemon=True).start()
        if self.platform.app_block_supported():
            self.refresh_block_list()
        self.refresh_weak_state()

    def _apply_adapters(self, ads):
        self.adapters = ads
        names = [a["name"] for a in ads]
        self.adapter_cb["values"] = names
        saved = self.cfg.get("last_adapter")
        if names:
            self.adapter_var.set(saved if saved in names else names[0])
        else:
            self.adapter_var.set("")
        self.refresh_net_status()

    def refresh_admin(self):
        if self.platform.is_admin():
            self.admin_var.set("● 权限 OK")
            self.admin_lbl.configure(foreground=self.GREEN)
            self.btn_elevate.configure(state="disabled")
        else:
            self.admin_var.set("● 普通权限 · 开关网络会失败")
            self.admin_lbl.configure(foreground=self.RED)
            self.btn_elevate.configure(state="normal")

    def refresh_net_status(self):
        name = self.adapter_var.get()
        ad = next((a for a in self.adapters if a["name"] == name), None)
        txt, color = "未选择网卡", self.MUTED
        if ad:
            kind = self.platform.status_kind(ad)
            virt = "（虚拟网卡）" if self.platform.is_virtual(ad) else ""
            if kind == "up":
                txt, color = "%s%s：已启用 · 已连接" % (name, virt), self.GREEN
            elif kind == "disabled":
                txt, color = "%s%s：已禁用（断网）" % (name, virt), self.RED
            elif kind == "absent":
                txt, color = "%s%s：网卡不存在" % (name, virt), self.MUTED
            else:
                txt, color = "%s%s：已启用 · 未连上" % (name, virt), self.ORANGE
        self.net_state_var.set(txt)
        self.set_dot(color)
        if name:
            self.cfg["last_adapter"] = name
            save_config(self.cfg)
        threading.Thread(target=self._ping_bg, daemon=True).start()

    def _ping_bg(self):
        alive = self.platform.ping_once()
        self.root.after(0, lambda: self.ping_var.set(
            "联网检测：%s" % ("可上网 ✓" if alive else "不通 ✗")))

    # ---------------- 模式一 ----------------
    def _targets(self, all_mode):
        if not all_mode:
            n = self.adapter_var.get()
            return [n] if n else []
        if IS_WIN and self.phys_only_var.get():
            picked, skipped = [], []
            for a in self.adapters:
                if self.platform.status_kind(a) == "absent":
                    continue
                (skipped if self.platform.is_virtual(a) else picked).append(a["name"])
            if skipped:
                self.log_line("已跳过虚拟/隧道网卡：%s" % "、".join(skipped), "warn")
            return picked
        return [a["name"] for a in self.adapters if self.platform.status_kind(a) != "absent"]

    def net_toggle(self, enable):
        if not self.ensure_admin():
            return
        names = self._targets(False)
        if not names:
            messagebox.showinfo("提示", "请先选择网卡")
            return
        self.stop_all_tasks()
        threading.Thread(target=self._net_worker, args=(names, enable, False), daemon=True).start()

    def net_toggle_all(self, enable):
        if not self.ensure_admin():
            return
        names = self._targets(True)
        if not names:
            messagebox.showinfo("提示", "没有可操作的网卡")
            return
        if not enable and not messagebox.askyesno(
                "确认", "将禁用 %d 张网卡，本机立即离线。确定？\n（%s）"
                        % (len(names), "、".join(names[:6]) + ("…" if len(names) > 6 else ""))):
            return
        self.stop_all_tasks()
        threading.Thread(target=self._net_worker, args=(names, enable, True), daemon=True).start()

    def _net_worker(self, names, enable, auto_restore):
        self._busy(True)
        ok_all = True
        for n in names:
            ok, out = self.platform.set_adapter(n, enable)
            if ok:
                self.log_line("%s %s：成功" % ("恢复" if enable else "断开", n), "ok")
            else:
                ok_all = False
                self.log_line("%s %s：失败 → %s" % ("恢复" if enable else "断开", n,
                                                    (out or "")[-160:].replace("\n", " ")), "err")
        self.root.after(1000, self.refresh_all)
        self.root.after(1100, lambda: self._busy(False))
        if not enable and auto_restore and self.auto_var.get() and ok_all:
            self.start_auto_restore(names)

    def start_auto_restore(self, names):
        try:
            sec = max(3, int(self.auto_sec_var.get() or 30))
        except Exception:
            sec = 30
        self.log_line("已开启自动恢复：%d 秒后恢复网络" % sec, "warn")
        threading.Thread(target=self._auto_worker, args=(sec, names), daemon=True).start()

    def _auto_worker(self, sec, names):
        for left in range(sec, 0, -1):
            if self._stop_event.is_set():
                self.root.after(0, lambda: self.auto_left_var.set(""))
                return
            self.root.after(0, lambda v=left: self.auto_left_var.set("自动恢复倒计时：%d 秒" % v))
            time.sleep(1)
        self.root.after(0, lambda: self.auto_left_var.set("正在自动恢复…"))
        self.log_line("倒计时结束，自动恢复网络", "warn")
        for n in names:
            self.platform.set_adapter(n, True)
        self.root.after(0, lambda: self.auto_left_var.set(""))
        self.root.after(1000, self.refresh_all)

    # ---------------- 模式二：循环断网 ----------------
    def loop_start(self):
        if not self.ensure_admin():
            return
        names = self._targets(True)
        if not names:
            messagebox.showinfo("提示", "没有可操作的网卡")
            return
        try:
            off = max(1, int(self.loop_off_var.get() or 10))
            on = max(1, int(self.loop_on_var.get() or 10))
            cnt = max(0, int(self.loop_cnt_var.get() or 5))
        except Exception:
            messagebox.showerror("参数错误", "请填写合法的数字")
            return
        if not self.loop_var.get() and cnt == 0:
            messagebox.showinfo("提示", "请勾选「启用」或设置轮数 > 0")
            return
        if not messagebox.askyesno("确认", "将对 %d 张网卡执行循环断网：断 %ds / 连 %ds%s。\n\n"
                                           "过程中本机网络会反复中断，确定？"
                                           % (len(names), off, on,
                                              "，共 %d 轮" % cnt if cnt else "，不限轮数")):
            return
        self._persist_opts()
        self.stop_all_tasks()
        self._stop_event.clear()
        self.btn_loop_start.configure(state="disabled")
        self.btn_loop_stop.configure(state="normal")
        threading.Thread(target=self._loop_worker, args=(names, off, on, cnt), daemon=True).start()

    def loop_stop(self):
        self._stop_event.set()
        self.log_line("收到停止指令，正在恢复网络…", "warn")
        self.root.after(0, lambda: self.loop_state_var.set("正在停止…"))

    def _loop_worker(self, names, off, on, cnt):
        rounds = 0
        while not self._stop_event.is_set():
            rounds += 1
            if cnt and rounds > cnt:
                break
            self.root.after(0, lambda r=rounds: self.loop_state_var.set("第 %d 轮：正在断网 %ds" % (r, off)))
            self.log_line("循环第 %d 轮 → 断网" % rounds, "warn")
            for n in names:
                self.platform.set_adapter(n, False)
            self.root.after(0, lambda r=rounds: self.loop_state_var.set("第 %d 轮：已断网 %ds" % (r, off)))
            if self._sleep_check(off):
                break
            self.root.after(0, lambda r=rounds: self.loop_state_var.set("第 %d 轮：正在恢复 %ds" % (r, on)))
            self.log_line("循环第 %d 轮 → 恢复" % rounds, "ok")
            for n in names:
                self.platform.set_adapter(n, True)
            if self._sleep_check(on):
                break
        for n in names:
            self.platform.set_adapter(n, True)
        self.root.after(0, lambda: (
            self.btn_loop_start.configure(state="normal"),
            self.btn_loop_stop.configure(state="disabled"),
            self.loop_state_var.set("已停止，网络已恢复"),
        ))
        self.log_line("循环任务结束，已恢复全部网卡", "ok")
        self.root.after(1000, self.refresh_all)

    def _sleep_check(self, sec):
        for _ in range(sec):
            if self._stop_event.is_set():
                return True
            time.sleep(1)
        return False

    def stop_all_tasks(self):
        self._stop_event.set()
        time.sleep(0.05)
        self._stop_event.clear()
        self.root.after(0, lambda: self.auto_left_var.set(""))

    # ---------------- 模式三：单应用（Windows） ----------------
    def pick_exe(self):
        ftype = [("可执行文件", "*.exe")] if IS_WIN else [("应用程序", "*.app"), ("所有文件", "*.*")]
        p = filedialog.askopenfilename(title="选择要控制的程序", filetypes=ftype)
        if p:
            self.exe_var.set(p)

    def pick_process(self):
        win = tk.Toplevel(self.root)
        win.title("选择正在运行的进程")
        win.geometry("660x440")
        win.transient(self.root)
        win.grab_set()
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        self.root.update_idletasks()
        search_var = tk.StringVar()
        ttk.Entry(frame, textvariable=search_var).pack(fill="x", pady=(0, 6))
        lst = tk.Listbox(frame, font=("Consolas", 9))
        lst.pack(fill="both", expand=True)
        procs = []

        def fill(ft=""):
            lst.delete(0, "end")
            f = ft.lower()
            for name, path in procs:
                if f in name.lower() or f in path.lower():
                    lst.insert("end", "%-28s | %s" % (name, path))

        def load():
            res = self.platform.list_processes()
            del procs[:]
            procs.extend(res)
            try:
                win.after(0, lambda: fill(search_var.get()))
            except Exception:
                pass

        def do_search(*_):
            fill(search_var.get())

        search_var.trace_add("write", do_search)

        def confirm(_=None):
            sel = lst.curselection()
            if not sel:
                return
            path = lst.get(sel[0]).split("|")[-1].strip()
            self.exe_var.set(path)
            win.destroy()

        lst.bind("<Double-Button-1>", confirm)
        ttk.Button(frame, text="确定", command=confirm).pack(pady=(8, 0))
        self.log_line("正在读取进程列表…")
        threading.Thread(target=load, daemon=True).start()

    def app_block(self):
        if not self.ensure_admin():
            return
        p = self.exe_var.get().strip().strip('"')
        if not p:
            messagebox.showinfo("提示", "请先选择或填写程序路径")
            return
        if not os.path.exists(p) and not messagebox.askyesno(
                "文件不存在", "%s\n\n路径不存在，仍要创建规则吗？" % p):
            return
        threading.Thread(target=self._app_block_bg, args=(p,), daemon=True).start()

    def _app_block_bg(self, p):
        self._busy(True)
        ok, detail = self.platform.block_app(p)
        if ok:
            self.cfg["last_exe"] = p
            save_config(self.cfg)
            self.log_line("已阻断：%s（出站+入站）" % p, "ok")
        else:
            out = detail if isinstance(detail, str) else ""
            for d, dok, o in (detail if isinstance(detail, list) else []):
                if not dok:
                    self.log_line("阻断失败(%s)：%s" % (d, (o or "")[-180:].replace("\n", " ")), "err")
            if out:
                self.log_line("阻断失败：%s" % (out or "")[:200], "err")
        self.root.after(0, self.refresh_block_list)
        self.root.after(500, lambda: self._busy(False))

    def app_unblock(self):
        if not self.ensure_admin():
            return
        p = self.exe_var.get().strip().strip('"')
        if not p:
            messagebox.showinfo("提示", "请先选择或填写程序路径")
            return
        threading.Thread(target=self._app_unblock_bg, args=(p,), daemon=True).start()

    def _app_unblock_bg(self, p):
        self._busy(True)
        ok, out = self.platform.unblock_app(p)
        if ok:
            self.log_line("已放行：%s" % p, "ok")
        else:
            self.log_line("放行失败：%s" % (out or "")[-200:].replace("\n", " "), "err")
        self.root.after(0, self.refresh_block_list)
        self.root.after(500, lambda: self._busy(False))

    def refresh_block_list(self):
        threading.Thread(target=lambda: self.root.after(
            0, lambda r=self.platform.list_rules(): self._fill_block_list(r)), daemon=True).start()

    def _fill_block_list(self, rules):
        if not hasattr(self, "block_list"):
            return
        self.block_list.delete(0, "end")
        self.row_map = {}
        self.rules = rules or []
        if not self.rules:
            self.block_list.insert("end", "  （无）当前没有被 NetSwitch 阻断的程序")
            return
        for r in self.rules:
            prog = r.get("program") or "(未知路径)"
            line = "  %s   [%s]  %s" % (r.get("name", ""), r.get("direction", "-"), prog)
            idx = self.block_list.index("end")
            self.block_list.insert("end", line)
            self.row_map[idx] = r

    def on_block_dbl(self, _=None):
        sel = self.block_list.curselection()
        if not sel:
            return
        r = self.row_map.get(sel[0])
        if r and r.get("program"):
            self.exe_var.set(r["program"])

    def clear_rules(self):
        if not self.ensure_admin():
            return
        rules = self.platform.list_rules()
        if not rules:
            messagebox.showinfo("提示", "当前没有需要清理的规则")
            return
        if not messagebox.askyesno("确认", "将删除 NetSwitch 创建的 %d 条阻断规则，确定？" % len(rules)):
            return
        threading.Thread(target=self._clear_rules_bg, args=(rules,), daemon=True).start()

    def _clear_rules_bg(self, rules):
        self._busy(True)
        fail = 0
        for r in rules:
            if not r.get("name"):
                continue
            ok, _ = self.platform.delete_rule(r["name"])
            if not ok:
                fail += 1
        self.log_line("清理完成：共 %d 条，失败 %d 条" % (len(rules), fail), "ok" if not fail else "warn")
        self.root.after(0, self.refresh_block_list)
        self.root.after(500, lambda: self._busy(False))

    # ---------------- 模式四：弱网 ----------------
    def _weak_profile(self):
        try:
            return {
                "dl_kbps": int(self.dl_var.get() or 0),
                "ul_kbps": int(self.ul_var.get() or 0),
                "latency_ms": int(self.lat_var.get() or 0),
                "jitter_ms": int(self.jit_var.get() or 0),
                "loss_pct": int(self.loss_var.get() or 0),
                "reorder_pct": int(self.reord_var.get() or 0),
            }
        except Exception:
            return {}

    def weak_start(self):
        if not self.ensure_admin():
            return
        prof = self._weak_profile()
        if not prof:
            messagebox.showerror("参数错误", "请填写合法的弱网参数")
            return
        self._persist_weak()
        self.btn_weak_on.configure(state="disabled")
        self.btn_weak_off.configure(state="normal")
        self.weak_state_var.set("正在开启弱网…")
        threading.Thread(target=self._weak_start_bg, args=(prof,), daemon=True).start()

    def _weak_start_bg(self, prof):
        ok, msg = self.platform.set_weak(prof)
        self._weak_active = ok
        self.root.after(0, lambda: self.weak_state_var.set(
            ("弱网运行中：%s" % self._weak_desc(prof)) if ok else ("开启失败：%s" % msg[:120])))
        if ok:
            self.log_line("弱网已开启 · %s" % self._weak_desc(prof), "ok")
            self.log_line(msg, "info")
        else:
            self.log_line("弱网开启失败：%s" % msg, "err")
            self.root.after(0, lambda: (self.btn_weak_on.configure(state="normal"),
                                        self.btn_weak_off.configure(state="disabled")))

    def _weak_desc(self, prof):
        parts = []
        if prof.get("dl_kbps"):
            parts.append("下行 %dkbps" % prof["dl_kbps"])
        if prof.get("ul_kbps"):
            parts.append("上行 %dkbps" % prof["ul_kbps"])
        if prof.get("latency_ms"):
            parts.append("延迟 %dms" % prof["latency_ms"])
        if prof.get("jitter_ms"):
            parts.append("抖动 %dms" % prof["jitter_ms"])
        if prof.get("loss_pct"):
            parts.append("丢包 %d%%" % prof["loss_pct"])
        if prof.get("reorder_pct"):
            parts.append("乱序 %d%%" % prof["reorder_pct"])
        return "、".join(parts) if parts else "全参数 0（等于不限速）"

    def weak_stop(self):
        self.weak_state_var.set("正在停止…")
        threading.Thread(target=self._weak_stop_bg, daemon=True).start()

    def _weak_stop_bg(self):
        ok, msg = self.platform.clear_weak()
        self._weak_active = False
        self.root.after(0, lambda: (self.btn_weak_on.configure(state="normal"),
                                    self.btn_weak_off.configure(state="disabled"),
                                    self.weak_state_var.set("弱网已停止" if ok else msg[:120])))
        self.log_line("弱网已停止：%s" % msg, "ok")

    def refresh_weak_state(self):
        active = self.platform.weak_active()
        self._weak_active = active
        self.root.after(0, lambda: (
            self.btn_weak_off.configure(state="normal" if active else "disabled"),
            self.btn_weak_on.configure(state="disabled" if active else "normal"),
            self.weak_state_var.set("弱网运行中" if active else ""),
        ))

    # ---------------- 权限 ----------------
    def do_elevate(self):
        if messagebox.askyesno("提权", "将以管理员(root)身份重新启动本程序，当前窗口会关闭。继续？"):
            if self.platform.elevate():
                self.on_close()
            else:
                messagebox.showerror("失败", "提权被拒绝或失败")

    def create_uac_free(self):
        if getattr(sys, "frozen", False):
            target, args = '"%s"' % sys.executable, ""
        else:
            target, args = '"%s"' % sys.executable, '"%s"' % os.path.abspath(__file__)
        ok, out = run_cmd('schtasks /create /tn "%s" /tr %s %s /sc once /st 00:00 /rl highest /f'
                          % (TASK_NAME, target, args))
        if not ok:
            tail = (out or "")[-300:].replace("\n", " ")
            self.log_line("创建计划任务失败：%s" % tail, "err")
            messagebox.showerror("失败", "创建计划任务失败：\n%s" % tail)
            return
        self.log_line("已创建计划任务 %s" % TASK_NAME, "ok")
        ok2, msg = self._make_shortcut()
        if ok2:
            messagebox.showinfo("完成", "已在桌面创建「NetSwitch 免UAC」快捷方式。\n"
                                        "以后双击它直接以管理员运行，不再弹 UAC 提示。")
        else:
            messagebox.showinfo("部分完成", "计划任务已创建（任务名 %s），\n"
                                            "但桌面快捷方式创建失败：%s\n\n"
                                            "可手动运行：schtasks /run /tn \"%s\""
                                            % (TASK_NAME, msg, TASK_NAME))

    def _make_shortcut(self):
        try:
            import win32com.client  # type: ignore
        except ImportError:
            return False, "缺少 pywin32"
        try:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            lnk = os.path.join(desktop, "%s 免UAC.lnk" % APP_NAME)
            sc = win32com.client.Dispatch("WScript.Shell").CreateShortCut(lnk)
            sc.TargetPath = "schtasks.exe"
            sc.Arguments = '/run /tn "%s"' % TASK_NAME
            sc.WorkingDirectory = _BASE
            if getattr(sys, "frozen", False):
                sc.IconLocation = sys.executable
            sc.Save()
            return True, lnk
        except Exception as e:
            return False, str(e)

    # ---------------- 热键 ----------------
    def _bind_hotkeys(self):
        def worker():
            try:
                keyboard.add_hotkey("ctrl+alt+k", lambda: self.root.after(0, lambda: self.net_toggle(False)))
                keyboard.add_hotkey("ctrl+alt+r", lambda: self.root.after(0, lambda: self.net_toggle(True)))
                keyboard.add_hotkey("ctrl+alt+s", lambda: self.root.after(0, self.loop_stop))
                self.log_line("全局热键已启用：Ctrl+Alt+K 断网 / Ctrl+Alt+R 恢复 / Ctrl+Alt+S 停止", "ok")
                keyboard.wait()
            except Exception as e:
                self.log_line("热键注册失败：%s" % e, "warn")
        threading.Thread(target=worker, daemon=True).start()

    # ---------------- 关闭 ----------------
    def on_close(self):
        self._stop_event.set()
        try:
            if self._weak_active:
                self.platform.clear_weak()
        except Exception:
            pass
        self._persist_opts()
        self._persist_weak()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = NetSwitchApp(root)
    admin = app.platform.is_admin()
    app.log_line("启动完成，权限：%s" % ("管理员/root" if admin else "普通用户（开关网络会失败）"),
                 "ok" if admin else "warn")
    if not admin:
        root.after(300, lambda: messagebox.showwarning(
            "建议以管理员运行",
            "当前不是管理员/root 身份，网卡开关、弱网等操作都会失败。\n\n"
            "可关闭后右键「以管理员身份运行」，或点右下角「以管理员/root 重启」。"))
    last = app.cfg.get("last_exe")
    if last and app.platform.app_block_supported():
        app.exe_var.set(last)
    root.mainloop()


if __name__ == "__main__":
    main()
