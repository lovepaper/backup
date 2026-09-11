# -*- coding: utf-8 -*-
"""
NetSwitch - PC 网络控制开关
面向测试工作：快速整机断网 / 只掐单个应用的网络 / 循环模拟弱网抖动

所有开关操作均需管理员权限。
"""

import os
import sys
import json
import time
import ctypes
import hashlib
import threading
import subprocess
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME = "NetSwitch"
APP_VERSION = "1.0.0"
RULE_PREFIX = "NetSwitch_Block_"
TASK_NAME = "NetSwitch_Admin_Launcher"

_BASE = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_BASE, "netswitch_config.json")

# 虚拟机/隧道类网卡，全部操作时默认跳过，避免搞挂 Docker / WSL / VMware / VPN
VIRTUAL_KEYWORDS = (
    "hyper-v", "vethernet", "vmware", "virtualbox", "vbox", "wsl",
    "tap-windows", "tap-win32", "zerotier", "tailscale", "openvpn",
    "virtual", "loopback", "npf_", "npcap", "docker", "bluetooth", "miniport",
)

# 全局热键依赖 keyboard 库，缺失时功能自动降级
try:
    import keyboard  # type: ignore
    HAS_KEYBOARD = True
except Exception:
    HAS_KEYBOARD = False


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate() -> bool:
    """以管理员身份重新拉起本程序"""
    try:
        if getattr(sys, "frozen", False):
            exe, arg = sys.executable, ""
        else:
            exe, arg = sys.executable, '"%s"' % os.path.abspath(__file__)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, arg, os.getcwd(), 1)
        return rc > 32
    except Exception:
        return False


def run_cmd(cmd, shell=True, timeout=25):
    """执行命令，返回 (ok, text)"""
    try:
        p = subprocess.run(
            cmd, shell=shell, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (p.stdout or b"") + (p.stderr or b"")
        text = ""
        for enc in ("utf-8", "gbk"):
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


def ps_json(script, timeout=25):
    """执行 PowerShell 脚本并解析 JSON 输出"""
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


def ps_lines(script, timeout=25):
    """执行 PowerShell 脚本并返回非空输出行"""
    ok, text = run_cmd([
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script
    ], shell=False, timeout=timeout)
    if not ok:
        return []
    return [l.strip() for l in text.splitlines() if l.strip()]


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
# 网卡操作（模式一：整机断网）
# --------------------------------------------------------------------------
def list_adapters():
    """返回 [{'name','desc','status'}]，status ∈ Up/Disabled/Disconnected/Not Present"""
    data = ps_json(
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


def is_virtual(ad):
    """判断是否为虚拟网卡/隧道网卡"""
    blob = ("%s %s" % (ad.get("name", ""), ad.get("desc", ""))).lower()
    return any(k in blob for k in VIRTUAL_KEYWORDS)


def status_kind(ad):
    """返回 up / disabled / disconnected / absent"""
    s = str(ad.get("status", "")).strip().lower()
    if s.startswith("disabled"):
        return "disabled"
    if s.startswith("not present"):
        return "absent"
    if s.startswith("up"):
        return "up"
    return "disconnected"


def set_adapter(name, enable):
    """启用/禁用单张网卡"""
    verb = "enable" if enable else "disable"
    return run_cmd('netsh interface set interface name="%s" admin=%s' % (name, verb), timeout=30)


def set_adapter_ps(name, enable):
    """netsh 失败时的兜底方案"""
    verb = "Enable-NetAdapter" if enable else "Disable-NetAdapter"
    ok, out = run_cmd(
        'powershell -NoProfile -ExecutionPolicy Bypass -Command '
        '%s -Name "%s" -Confirm:$false -ErrorAction Stop' % (verb, name)
    )
    return ok, out


def toggle_adapter_smart(name, enable):
    """先 netsh，失败再 PowerShell"""
    ok, out = set_adapter(name, enable)
    if not ok:
        ok2, out2 = set_adapter_ps(name, enable)
        return ok2, out + " || " + out2
    return ok, out


def ping_once(host="223.5.5.5", timeout_ms=1500):
    ok, out = run_cmd("ping -n 1 -w %d %s" % (timeout_ms, host), timeout=12)
    return ("TTL=" in out.upper()) or ("time=" in out.lower())


# --------------------------------------------------------------------------
# 防火墙规则（模式二：单应用断网）
# --------------------------------------------------------------------------
def rule_name_for(exe_path):
    h = hashlib.md5(exe_path.lower().encode("utf-8")).hexdigest()[:8]
    return RULE_PREFIX + h


def _missing(out):
    low = (out or "").lower()
    return ("no rules match" in low) or ("没有匹配的规则" in out) or ("找不到" in out)


def app_blocked(exe_path):
    ok, out = run_cmd('netsh advfirewall firewall show rule name="%s"' % rule_name_for(exe_path))
    return bool(ok) and not _missing(out)


def block_app(exe_path):
    """阻断指定 exe 的出站 + 入站"""
    rn = rule_name_for(exe_path)
    detail = []
    for direction in ("out", "in"):
        ok, out = run_cmd(
            'netsh advfirewall firewall add rule name="%s" dir=%s action=block '
            'program="%s" enable=yes profile=any' % (rn, direction, exe_path)
        )
        detail.append((direction, ok, out))
    return all(d[1] for d in detail), detail


def unblock_app(exe_path):
    ok, out = run_cmd('netsh advfirewall firewall delete rule name="%s"' % rule_name_for(exe_path))
    if not ok and _missing(out):
        return True, "规则本就不存在"
    return ok, out


def list_netswitch_rules():
    """返回 [{'name','direction','program'}]"""
    data = ps_json(
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


def delete_rule_by_name(name):
    return run_cmd('netsh advfirewall firewall delete rule name="%s"' % name)


def list_processes():
    """返回 [(basename, path)]，去重排序"""
    data = ps_json(
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
# GUI
# --------------------------------------------------------------------------
class NetSwitchApp:
    def __init__(self, root):
        self.root = root
        self.root.title("%s v%s - PC 网络控制开关" % (APP_NAME, APP_VERSION))
        self.root.geometry("780x680")
        self.root.minsize(720, 620)

        self.cfg = load_config()
        self.adapters = []
        self.rules = []
        self.row_map = {}        # listbox index -> rule dict
        self._stop_event = threading.Event()
        self._worker = None

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

    # ---------------- 样式 ----------------
    def _build_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=self.BG, foreground=self.FG, fieldbackground="#ffffff")
        st.configure("TFrame", background=self.BG)
        st.configure("Card.TFrame", background=self.CARD)
        st.configure("TLabel", background=self.BG, foreground=self.FG, font=("Microsoft YaHei UI", 10))
        st.configure("Card.TLabel", background=self.CARD, foreground=self.FG)
        st.configure("Title.TLabel", background=self.CARD, foreground=self.FG,
                     font=("Microsoft YaHei UI", 12, "bold"))
        st.configure("Muted.TLabel", background=self.CARD, foreground=self.MUTED,
                     font=("Microsoft YaHei UI", 9))
        st.configure("TButton", font=("Microsoft YaHei UI", 10), padding=6)
        st.configure("TCheckbutton", background=self.CARD, foreground=self.FG)
        st.configure("TCombobox", font=("Microsoft YaHei UI", 10))
        st.configure("TEntry", font=("Microsoft YaHei UI", 10))
        st.configure("TSpinbox", font=("Microsoft YaHei UI", 10))

    def _card(self):
        return ttk.Frame(self.root, style="Card.TFrame", padding=14)

    # ---------------- UI ----------------
    def _build_ui(self):
        top = self._card()
        top.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(top, text="%s   PC 网络控制开关" % APP_NAME, style="Title.TLabel").pack(side="left")
        self.admin_var = tk.StringVar(value="")
        self.admin_lbl = ttk.Label(top, textvariable=self.admin_var, style="Muted.TLabel")
        self.admin_lbl.pack(side="right")
        ttk.Label(top, text="v%s" % APP_VERSION, style="Muted.TLabel").pack(side="right", padx=(0, 12))

        self._build_net_card()
        self._build_loop_card()
        self._build_app_card()
        self._build_log_card()

        bot = ttk.Frame(self.root, padding=(12, 4))
        bot.pack(fill="x")
        tip = "热键：Ctrl+Alt+K 断网 / Ctrl+Alt+R 恢复 / Ctrl+Alt+S 停止任务" if HAS_KEYBOARD \
            else "（未检测到 keyboard 库，全局热键不可用）"
        ttk.Label(bot, text=tip, style="Muted.TLabel").pack(side="left")
        self.btn_task = ttk.Button(bot, text="创建免UAC快捷方式", command=self.create_uac_free)
        self.btn_task.pack(side="right")
        self.btn_elevate = ttk.Button(bot, text="以管理员重启", command=self.do_elevate)
        self.btn_elevate.pack(side="right", padx=(0, 8))

    def _build_net_card(self):
        c = self._card()
        c.pack(fill="x", padx=12, pady=6)
        ttk.Label(c, text="模式一 · 整机断网（禁用 / 启用网卡）", style="Title.TLabel").pack(anchor="w")

        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x", pady=(10, 6))
        ttk.Label(row, text="网卡：", style="Card.TLabel").pack(side="left")
        self.adapter_var = tk.StringVar()
        self.adapter_cb = ttk.Combobox(row, textvariable=self.adapter_var, state="readonly", width=44)
        self.adapter_cb.pack(side="left", padx=(4, 10))
        self.adapter_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_net_status())
        ttk.Button(row, text="刷新", command=self.refresh_all, width=8).pack(side="left")
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
        self.btn_off_all = ttk.Button(bro, text="断开全部网卡", command=lambda: self.net_toggle_all(False),
                                      width=14)
        self.btn_off_all.pack(side="left", padx=(10, 0))
        self.btn_on_all = ttk.Button(bro, text="恢复全部网卡", command=lambda: self.net_toggle_all(True),
                                     width=14)
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
            for b in (self.btn_off, self.btn_on, self.btn_off_all, self.btn_on_all,
                      self.btn_block, self.btn_unblock, self.btn_clear_rules):
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

    def ensure_admin(self):
        if is_admin():
            return True
        messagebox.showwarning(
            "需要管理员权限",
            "开关网卡 / 改防火墙规则必须管理员权限。\n\n"
            "请点右下角「以管理员重启」，或关闭后右键 → 以管理员身份运行。"
        )
        return False

    # ---------------- 刷新 ----------------
    def refresh_all(self):
        self.refresh_admin()
        threading.Thread(target=lambda: self.root.after(
            0, lambda: self._apply_adapters(list_adapters())), daemon=True).start()
        self.refresh_block_list()

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
        if is_admin():
            self.admin_var.set("● 管理员权限 OK")
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
            kind = status_kind(ad)
            virt = "（虚拟网卡）" if is_virtual(ad) else ""
            if kind == "up":
                txt, color = "%s%s：已启用 · 已连接" % (name, virt), self.GREEN
            elif kind == "disabled":
                txt, color = "%s%s：已禁用（断网）" % (name, virt), self.RED
            elif kind == "absent":
                txt, color = "%s%s：网卡不存在" % (name, virt), self.MUTED
            else:
                txt, color = "%s%s：已启用 · 网线/WiFi 未连上" % (name, virt), self.ORANGE
        self.net_state_var.set(txt)
        self.set_dot(color)
        if name:
            self.cfg["last_adapter"] = name
            save_config(self.cfg)
        threading.Thread(target=self._ping_bg, daemon=True).start()

    def _ping_bg(self):
        alive = ping_once()
        self.root.after(0, lambda: self.ping_var.set(
            "联网检测：%s" % ("可上网 ✓" if alive else "不通 ✗")))

    # ---------------- 模式一 ----------------
    def _targets(self, all_mode):
        if not all_mode:
            n = self.adapter_var.get()
            return [n] if n else []
        if self.phys_only_var.get():
            picked, skipped = [], []
            for a in self.adapters:
                if status_kind(a) == "absent":
                    continue
                (skipped if is_virtual(a) else picked).append(a["name"])
            if skipped:
                self.log_line("已跳过虚拟/隧道网卡：%s" % "、".join(skipped), "warn")
            return picked
        return [a["name"] for a in self.adapters if status_kind(a) != "absent"]

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
            ok, out = toggle_adapter_smart(n, enable)
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

    # ---------------- 自动恢复 ----------------
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
            toggle_adapter_smart(n, True)
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
            self.root.after(0, lambda r=rounds: self.loop_state_var.set(
                "第 %d 轮：正在断网 %ds" % (r, off)))
            self.log_line("循环第 %d 轮 → 断网" % rounds, "warn")
            for n in names:
                toggle_adapter_smart(n, False)
            self.root.after(0, lambda r=rounds: self.loop_state_var.set(
                "第 %d 轮：已断网 %ds" % (r, off)))
            if self._sleep_check(off):
                break

            self.root.after(0, lambda r=rounds: self.loop_state_var.set(
                "第 %d 轮：正在恢复 %ds" % (r, on)))
            self.log_line("循环第 %d 轮 → 恢复" % rounds, "ok")
            for n in names:
                toggle_adapter_smart(n, True)
            if self._sleep_check(on):
                break

        for n in names:
            toggle_adapter_smart(n, True)
        self.root.after(0, lambda: (
            self.btn_loop_start.configure(state="normal"),
            self.btn_loop_stop.configure(state="disabled"),
            self.loop_state_var.set("已停止，网络已恢复"),
        ))
        self.log_line("循环任务结束，已恢复全部网卡", "ok")
        self.root.after(1000, self.refresh_all)

    def _sleep_check(self, sec):
        """可中断的 sleep，返回 True 表示被中断"""
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

    # ---------------- 模式三：单应用 ----------------
    def pick_exe(self):
        p = filedialog.askopenfilename(title="选择要控制的程序",
                                       filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
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
            res = list_processes()
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
        ok, detail = block_app(p)
        if ok:
            self.cfg["last_exe"] = p
            save_config(self.cfg)
            self.log_line("已阻断：%s（出站+入站）" % p, "ok")
        else:
            for d, dok, out in detail:
                if not dok:
                    self.log_line("阻断失败(%s)：%s" % (d, (out or "")[-180:].replace("\n", " ")), "err")
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
        ok, out = unblock_app(p)
        if ok:
            self.log_line("已放行：%s" % p, "ok")
        else:
            self.log_line("放行失败：%s" % (out or "")[-200:].replace("\n", " "), "err")
        self.root.after(0, self.refresh_block_list)
        self.root.after(500, lambda: self._busy(False))

    def refresh_block_list(self):
        threading.Thread(target=lambda: self.root.after(
            0, lambda r=list_netswitch_rules(): self._fill_block_list(r)), daemon=True).start()

    def _fill_block_list(self, rules):
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
        rules = list_netswitch_rules()
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
            ok, _ = delete_rule_by_name(r["name"])
            if not ok:
                fail += 1
        self.log_line("清理完成：共 %d 条，失败 %d 条" % (len(rules), fail), "ok" if not fail else "warn")
        self.root.after(0, self.refresh_block_list)
        self.root.after(500, lambda: self._busy(False))

    # ---------------- 权限 ----------------
    def do_elevate(self):
        if messagebox.askyesno("提权", "将以管理员身份重新启动本程序，当前窗口会关闭。继续？"):
            if elevate():
                self.on_close()
            else:
                messagebox.showerror("失败", "提权被拒绝或失败")

    def create_uac_free(self):
        """创建计划任务 + 桌面快捷方式，之后双击不再弹 UAC"""
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
        except Exception:
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
                keyboard.add_hotkey("ctrl+alt+k", lambda: self.root.after(
                    0, lambda: self.net_toggle(False)))
                keyboard.add_hotkey("ctrl+alt+r", lambda: self.root.after(
                    0, lambda: self.net_toggle(True)))
                keyboard.add_hotkey("ctrl+alt+s", lambda: self.root.after(0, self.loop_stop))
                self.log_line("全局热键已启用：Ctrl+Alt+K 断网 / Ctrl+Alt+R 恢复 / Ctrl+Alt+S 停止", "ok")
                keyboard.wait()
            except Exception as e:
                self.log_line("热键注册失败：%s" % e, "warn")
        threading.Thread(target=worker, daemon=True).start()

    # ---------------- 关闭 ----------------
    def on_close(self):
        self._stop_event.set()
        self._persist_opts()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = NetSwitchApp(root)
    app.log_line("启动完成，权限：%s" % ("管理员" if is_admin() else "普通用户（开关网络会失败）"),
                 "ok" if is_admin() else "warn")
    if not is_admin():
        root.after(300, lambda: messagebox.showwarning(
            "建议以管理员运行",
            "当前不是管理员身份，网卡开关和防火墙规则都会失败。\n\n"
            "可关闭后右键「以管理员身份运行」，或点右下角「以管理员重启」。"))
    last = app.cfg.get("last_exe")
    if last:
        app.exe_var.set(last)
    root.mainloop()


if __name__ == "__main__":
    main()
