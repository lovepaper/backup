# -*- coding: utf-8 -*-
"""
Windows 弱网引擎（实验性）· 基于 pydivert / WinDivert

对经过本机的所有数据包做系统级弱网模拟：
  - 带宽限制（下行/上行，令牌桶近似）
  - 延迟 + 抖动（队列 + 定时重注入）
  - 丢包（随机丢弃）
  - 乱序（概率性额外延迟，使后发先至）

需要管理员权限（WinDivert 要加载内核驱动）。
本模块被 netswitch.py 以 lazy import 调用，加载/运行失败不影响其它功能。
"""

import threading
import time
import random
import heapq

try:
    import pydivert  # 自带 WinDivert 驱动二进制
    HAS_PYDIVERT = True
except Exception:
    HAS_PYDIVERT = False

_engine = None  # 当前运行的引擎实例
_engine_lock = threading.Lock()


class WeakEngine:
    def __init__(self, profile):
        self.profile = profile
        self.stop_ev = threading.Event()
        self.w = None
        self.worker = None
        self.sender = None
        # 带宽令牌桶（按秒预算，允许突发到 1 秒量）
        self._bucket = {"bytes": 0.0, "ts": time.time()}
        self._bl = threading.Lock()
        # 延迟发送堆 (send_at, packet)
        self._heap = []
        self._hl = threading.Lock()
        self.stats = {"dropped": 0, "sent": 0}

    # ---- 带宽令牌桶：返回 True 表示该包允许通过 ----
    def _allow_bw(self, nbytes, kbps):
        if not kbps or kbps <= 0:
            return True
        rate = kbps * 125.0  # kbps -> bytes/s
        now = time.time()
        with self._bl:
            elapsed = now - self._bucket["ts"]
            if elapsed > 0:
                self._bucket["bytes"] = max(0.0, self._bucket["bytes"] - rate * elapsed)
                self._bucket["ts"] = now
            if self._bucket["bytes"] + nbytes <= rate:
                self._bucket["bytes"] += nbytes
                return True
            return False

    def start(self):
        if not HAS_PYDIVERT:
            raise RuntimeError("未安装 pydivert（请 pip install pydivert）")
        self.w = pydivert.WinDivert("true")  # 捕获所有 IPv4/IPv6 包
        self.w.open()
        self.worker = threading.Thread(target=self._capture, daemon=True)
        self.sender = threading.Thread(target=self._send, daemon=True)
        self.worker.start()
        self.sender.start()

    def stop(self):
        self.stop_ev.set()
        try:
            if self.w is not None:
                self.w.close()
        except Exception:
            pass

    def _capture(self):
        p = self.profile
        loss = (p.get("loss_pct") or 0) / 100.0
        jitter = p.get("jitter_ms") or 0
        latency = p.get("latency_ms") or 0
        reorder = (p.get("reorder_pct") or 0) / 100.0
        # 上行/下行预算：简化为统一用下行预算做总令牌桶（系统级近似）
        bw = p.get("dl_kbps") or 0
        try:
            for packet in self.w:
                if self.stop_ev.is_set():
                    break
                n = len(packet.raw)
                # 丢包
                if loss > 0 and random.random() < loss:
                    self.stats["dropped"] += 1
                    continue
                # 带宽限制
                if not self._allow_bw(n, bw):
                    self.stats["dropped"] += 1
                    continue
                # 延迟 + 抖动
                delay = latency / 1000.0
                if jitter > 0:
                    delay += random.uniform(-jitter, jitter) / 1000.0
                # 乱序：概率性再叠加一段延迟，使后发的包先到
                if reorder > 0 and random.random() < reorder:
                    delay += random.uniform(latency, latency + 200) / 1000.0
                if delay < 0:
                    delay = 0
                send_at = time.time() + delay
                with self._hl:
                    heapq.heappush(self._heap, (send_at, packet))
        except Exception:
            # 迭代在 stop() 调用 w.close() 后结束，异常多数为正常退出
            pass

    def _send(self):
        while not self.stop_ev.is_set():
            with self._hl:
                if self._heap and self._heap[0][0] <= time.time():
                    _, packet = heapq.heappop(self._heap)
                else:
                    packet = None
            if packet is not None:
                try:
                    self.w.send(packet)
                    self.stats["sent"] += 1
                except Exception:
                    pass
            else:
                time.sleep(0.002)


def start_weak(profile):
    """启动弱网，返回 (ok, msg)"""
    if not HAS_PYDIVERT:
        return False, "未检测到 pydivert 库，无法在 Windows 上做弱网（pip install pydivert）"
    global _engine
    with _engine_lock:
        if _engine is not None and not _engine.stop_ev.is_set():
            return False, "已有弱网在运行，请先停止"
        try:
            eng = WeakEngine(profile)
            eng.start()
            _engine = eng
        except Exception as e:
            return False, "弱网启动失败：%s" % e
    return True, "弱网已开启（系统级，所有流量受影响）"


def stop_weak():
    """停止弱网，返回 (ok, msg)"""
    global _engine
    with _engine_lock:
        if _engine is None:
            return True, "当前没有运行中的弱网"
        eng = _engine
        _engine = None
    eng.stop()
    return True, "弱网已停止，网络已恢复"


def is_active():
    return _engine is not None and not _engine.stop_ev.is_set()


def get_stats():
    if _engine is None:
        return {"dropped": 0, "sent": 0, "active": False}
    return {"dropped": _engine.stats["dropped"],
            "sent": _engine.stats["sent"],
            "active": not _engine.stop_ev.is_set()}
