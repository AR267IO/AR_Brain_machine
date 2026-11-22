# -*- coding: utf-8 -*-
"""
后端引擎。

职责只有三件：把 `dvc_auto.py` 当子进程跑起来、把它的输出整理成事件、把事件
广播给所有订阅者（SSE 连接）。这里不碰 tkinter，也不碰 win32，前端换了它不用动。

dvc_auto.py 保持完全独立，仍是能单独双击运行的程序。
"""
import collections
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if not getattr(sys, "frozen", False):
    sys.path.insert(0, HERE)
import window as W  # noqa: E402
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)         # 打包后：exe 旁边
else:
    BASE = os.path.dirname(HERE)                   # 新建文件夹
AUTO = os.path.join(BASE, "auto")

# [12:40:05] INFO  xxx   /   [12:40:05] WARN  xxx
LINE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s+(\w+)\s*(.*)$")
# dvc_auto.py 在高速刷价时直接吐这种裸行，不进日志、只用来更新价格卡
PRICE_RE = re.compile(r"^PRICE\s+(\d+)$")
# dvc_auto.py 抢到一件装备就打一行 PURCHASE helmet 241500，同样不进日志
PURCHASE_RE = re.compile(r"^PURCHASE\s+(helmet|armor)\s+(\d+)$")

# 日志里跟余额有关的行，前端不用解析，由后端顺手抓出来。
# 必须要求「余额」后面直接跟空格和三位以上的数字：失败那行是
# 「余额读不出来（…置信 0.25）」，「0.25」的 0 会被宽正则误当成余额。
BAL_RE = re.compile(r"余额\s+([\d,]{3,})")

LEVELS = ("STEP", "INFO", "WARN", "FATAL", "ACT")


def find_config():
    for p in (os.path.join(AUTO, "config.json"),
              os.path.join(BASE, "config.json"),
              os.path.join(BASE, "src", "标定程序", "config.json")):
        if os.path.exists(p):
            return p
    return None


def read_config():
    """读 config.json。读不出来就给一份空壳，前端不至于白屏。"""
    path = find_config()
    data = {"path": path, "regions": {}, "options": {}, "screen": None}
    if not path:
        return data
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        data["regions"] = raw.get("regions", {}) or {}
        data["options"] = raw.get("options", {}) or {}
        data["screen"] = raw.get("screen") or {
            "w": raw.get("screen_w"), "h": raw.get("screen_h"),
            "dpi": raw.get("dpi"),
        }
        data["mtime"] = os.path.getmtime(path)
        data["partial"] = bool(raw.get("partial"))
        data["calibrated"] = raw.get("calibrated_at") or raw.get("calibrated") or ""
    except Exception as e:
        data["error"] = str(e)
    return data


class Engine:
    def __init__(self):
        self.proc = None
        self.mode = "idle"          # idle | running | calibrating
        self.round_no = 0
        self.balance = {"text": "", "value": None, "conf": 0.0,
                        "ok": False, "ts": 0.0, "digits": 0}
        # 刷价时盯的那个「最低行价格」，由 dvc_auto.py 的 PRICE 裸行喂进来
        self.price = {"text": "", "value": None, "ok": False, "ts": 0.0}
        self.cfg = read_config()
        self.logs = collections.deque(maxlen=500)
        self.subs = []
        self.lock = threading.Lock()
        self._started_at = 0.0
        self._calib_before = 0.0
        self.pin = W.Pin()
        self.win_hwnd = None      # main.py 开窗后回填，Pin 用它，不靠前台窗口猜
        # 「抢购版」= exe 名带这三个字，或者环境变量强制。只跑 B，不读余额
        self.buy_only = ("抢购版" in os.path.basename(sys.executable)
                         or os.environ.get("GLM_BUY_ONLY") == "1")
        # 只有自己钉的窗口才在结束时还原：用户手动点「置顶」去看日志时
        # 跑一次读余额，不该把窗口弹回去
        self.run_pinned = False
        # 本轮抢到的数量和最近成交价，开始运行前清零
        self.stats = {"helmet": 0, "armor": 0, "last_price": None}
        self.purchases = collections.deque(maxlen=300)

    # -- 状态 ------------------------------------------------------------
    @property
    def running(self):
        return self.mode == "running" and self.proc is not None

    def snapshot(self):
        return {
            "mode": self.mode,
            "running": self.running,
            "pinned": self.pin.active,
            "round": self.round_no,
            "elapsed": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "balance": self.balance,
            "buy_only": self.buy_only,
            "price": self.price,
            "stats": dict(self.stats),
            "purchases": list(self.purchases)[-120:],
            "config": {
                "path": self.cfg.get("path"),
                "screen": self.cfg.get("screen"),
                "count": len(self.cfg.get("regions") or {}),
                "partial": self.cfg.get("partial", False),
                "calibrated": self.cfg.get("calibrated", ""),
                "options": self.cfg.get("options") or {},
                "error": self.cfg.get("error"),
            },
            "regions": self.cfg.get("regions") or {},
            "logs": list(self.logs)[-200:],
        }

    # -- 订阅 ------------------------------------------------------------
    def subscribe(self):
        q = collections.deque(maxlen=300)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            try:
                self.subs.remove(q)
            except ValueError:
                pass

    def _note_price(self, v):
        """高频刷价只在乎「有没有变」，同一个数字不重复广播。"""
        if self.price.get("value") == int(v):
            return
        self.price = {"text": str(v), "value": int(v), "ok": True,
                      "ts": time.time()}
        self._emit("price", self.price)

    def _note_purchase(self, kind, price):
        """抢到一件。kind 是 helmet / armor，price 是这次成交价。"""
        self.stats[str(kind)] = int(self.stats.get(str(kind), 0)) + 1
        self.stats["last_price"] = int(price)
        rec = {
            "t": time.strftime("%H:%M:%S"),
            "kind": "头盔" if kind == "helmet" else "护甲",
            "price": int(price),
            "no": self.stats[str(kind)],
        }
        self.purchases.append(rec)
        self._emit("stats", dict(self.stats))
        self._emit("purchase", rec)

    def _emit(self, kind, payload):
        item = (kind, payload)
        with self.lock:
            for q in list(self.subs):
                q.append(item)

    def _log(self, msg, level="INFO"):
        rec = {"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg}
        self.logs.append(rec)
        self._emit("log", rec)
        if level in ("WARN", "FATAL"):
            print("[%s] %s %s" % (rec["t"], level, msg), flush=True)

    # -- 余额 ------------------------------------------------------------
    def _note_balance(self, text):
        """日志里出现「余额 640,350」这类字样时顺手更新卡片。"""
        m = BAL_RE.search(text)
        if not m:
            return
        raw = m.group(1).replace(",", "")
        try:
            val = int(raw)
        except ValueError:
            return
        self.balance = {"text": m.group(1), "value": val, "conf": 1.0,
                        "ok": True, "ts": time.time(), "digits": len(raw)}
        self._emit("balance", self.balance)

    # -- 动作 ------------------------------------------------------------
    def start(self, args=None):
        if self.mode != "idle":
            return {"ok": False, "err": "已经在运行"}
        args = list(args or ["--live"])
        if self.buy_only and "--buy-only" not in args:
            args.append("--buy-only")
        # 新一轮从零开始；上一轮的计数留在界面上直到这次真的开跑
        self.stats = {"helmet": 0, "armor": 0, "last_price": None}
        self._emit("stats", self.stats)
        py = sys.executable
        if getattr(sys, "frozen", False):
            # 打包后 py 就是本程序，靠 --run-auto 分派回 dvc_auto
            cfgp = self.cfg.get("path") or ""
            cmd = [py, "--run-auto"] + (["--config", cfgp] if cfgp else []) + args
        else:
            cmd = [py, os.path.join(AUTO, "dvc_auto.py")] + args
        # 子进程的 stdout 是管道，Python 默认按系统 locale(cp936) 写，这里却按
        # utf-8 读，中文就会变成乱码。给子进程把编码钉死成 utf-8。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=AUTO, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, encoding="utf-8",
                errors="replace", bufsize=1, env=env,
                creationflags=0x08000000)
        except Exception as e:
            self._log("启动失败：%s" % e, "FATAL")
            return {"ok": False, "err": str(e)}

        self.mode = "running"
        if "--live" in args:
            self.round_no += 1
            # 真正开跑才把窗口钉在最前并缩成小窗，读一次余额不动窗口
            pinned = self.pin.enter(*W.work_size(), hwnd=self.win_hwnd)
            self.run_pinned = pinned
            if pinned:
                threading.Thread(target=self._pin_keeper, daemon=True).start()
            self._emit("pin", {"on": pinned})
        self._started_at = time.time()
        self._log("读一次余额（不会点击）" if args == ["--probe"]
                  else "开始运行 " + " ".join(args), "STEP")
        self._emit("state", {"mode": self.mode, "round": self.round_no})
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        return {"ok": True}

    def _reader(self, proc):
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                mp = PRICE_RE.match(line)
                if mp:
                    self._note_price(int(mp.group(1)))
                    continue
                mp = PURCHASE_RE.match(line)
                if mp:
                    self._note_purchase(mp.group(1), int(mp.group(2)))
                    continue
                m = LINE_RE.match(line)
                if m:
                    t, lv, msg = m.groups()
                    lv = lv if lv in LEVELS else "INFO"
                else:
                    t, lv, msg = time.strftime("%H:%M:%S"), "INFO", line
                rec = {"t": t, "level": lv, "msg": msg}
                self.logs.append(rec)
                self._emit("log", rec)
                if "余额" in msg:
                    self._note_balance(msg)
        except Exception:
            pass
        code = -1
        try:
            code = proc.wait()
        except Exception:
            pass
        self.proc = None
        self.mode = "idle"
        self._started_at = 0.0
        if self.run_pinned and self.pin.active:
            self.pin.leave()
            self._emit("pin", {"on": False})
        self.run_pinned = False
        # 窗口刚从工作态放大回来，WebView2 还在按新尺寸重排。
        # 这时候抢前台会把它打断，界面上就是一大块白。等尺寸落定再拉前。
        time.sleep(0.45)
        W.raise_once(self.win_hwnd)
        self._log("进程结束，返回码 %d" % code, "STEP" if code == 0 else "WARN")
        self._emit("state", {"mode": self.mode, "round": self.round_no, "code": code})

    def stop(self):
        if self.proc is None:
            return {"ok": False, "err": "没有在运行"}
        try:
            self.proc.terminate()
        except Exception:
            pass
        self._log("手动停止", "WARN")
        return {"ok": True}

    def win_size(self):
        """开窗尺寸。整个进程只开一次窗、永不 resize —— 原因见 window.idle_size()。"""
        import window as W
        return W.idle_size()

    def force_repaint(self):
        """放大窗口之后 WebView2 有时候整块不跟手，界面上留一大片白。
        让页面自己重载一次是最干脆的恢复手段：日志、统计、余额全在
        /api/state 里，重载不会丢状态。"""
        try:
            import webview
            wins = getattr(webview, "windows", None) or []
            if not wins:
                return {"ok": False, "err": "还没有窗口"}
            import window as W
            r = W.rect_of(self.win_hwnd)
            if not r:
                return {"ok": False, "err": "拿不到窗口矩形"}
            w, h = r[2], r[3]
            # 尺寸抖动：pywebview 发现尺寸没变就不会通知 WebView2，
            # 先改成 +2 再改回来，逼它把绘制区域重算一遍
            W.webview_place(w + 2, h + 2, r[0], r[1])
            time.sleep(0.25)
            W.webview_place(w, h, r[0], r[1])
            return {"ok": True, "rect": [r[0], r[1], w, h]}
        except Exception as e:
            return {"ok": False, "err": str(e)}

    def _pin_keeper(self):
        """工作态期间每 10 秒重申一次置顶，防止被全屏程序之类的抢走。
        用 NOACTIVATE，不会把焦点从小窗抢走。"""
        while self.pin.active and self.mode != "idle":
            self.pin.keep_top()
            time.sleep(10)

    def set_pin(self, on):
        """手动置顶/取消。只动窗口层级和大小，跟自动化的起停无关。"""
        if on:
            if self.pin.active:
                return {"ok": True, "pinned": True}
            ok = self.pin.enter(*W.work_size(), hwnd=self.win_hwnd)
            if ok:
                threading.Thread(target=self._pin_keeper, daemon=True).start()
            self._emit("pin", {"on": self.pin.active})
            return {"ok": ok, "pinned": self.pin.active}
        if not self.pin.active:
            return {"ok": True, "pinned": False}
        self.pin.leave()
        self._emit("pin", {"on": False})
        time.sleep(0.45)
        W.raise_once(self.win_hwnd)
        return {"ok": True, "pinned": False}

    def probe(self):
        """只读一次余额，不点击。"""
        if self.mode != "idle":
            return {"ok": False, "err": "运行中不能读余额"}
        return self.start(["--probe"])

    def calibrate(self):
        if self.mode != "idle":
            return {"ok": False, "err": "运行中不能标定"}
        calib = os.path.join(BASE, "src", "标定程序", "dvc_calibrate.py")
        frozen = getattr(sys, "frozen", False)
        if not frozen and not os.path.exists(calib):
            return {"ok": False, "err": "找不到 " + calib}
        self.mode = "calibrating"
        try:
            self._calib_before = os.path.getmtime(self.cfg.get("path") or "")
        except OSError:
            self._calib_before = 0.0
        self._log("打开标定覆盖层，框完保存后会自动回来", "STEP")
        self._emit("state", {"mode": self.mode})

        def run():
            try:
                if getattr(sys, "frozen", False):
                    calib_cmd = [sys.executable, "--run-calib"]
                else:
                    calib_cmd = [sys.executable, calib]
                # 打包后没有 src\\标定程序 这个目录，cwd 只能用安装根目录
                calib_cwd = BASE if getattr(sys, "frozen", False) else os.path.dirname(calib)
                p = subprocess.Popen(calib_cmd,
                                     cwd=calib_cwd,
                                     creationflags=0x08000000)
                p.wait()
            except Exception as e:
                self._log("标定程序没能启动：%s" % e, "FATAL")
            after = 0.0
            try:
                after = os.path.getmtime(self.cfg.get("path") or "")
            except OSError:
                pass
            saved = after != self._calib_before
            self.cfg = read_config()
            self.mode = "idle"
            if saved:
                self._log("标定已保存，重新读取 %d 个区域"
                          % len(self.cfg.get("regions") or {}), "STEP")
            else:
                self._log("标定已放弃，config.json 没有改动", "WARN")
            self._emit("state", {"mode": self.mode, "calibrated": saved})
            self._emit("config", self.config_payload())

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def config_payload(self):
        """配置的对外样子。界面上只给摘要，不再逐个区域列，所以这里也不带 regions。"""
        return {
            "path": self.cfg.get("path"),
            "screen": self.cfg.get("screen"),
            "count": len(self.cfg.get("regions") or {}),
            "partial": self.cfg.get("partial", False),
            "calibrated": self.cfg.get("calibrated", ""),
        }

    def reload_config(self):
        self.cfg = read_config()
        self._emit("config", self.config_payload())
        return {"ok": True, "count": len(self.cfg.get("regions") or {})}
