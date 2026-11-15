# -*- coding: utf-8 -*-
r"""
三角洲自动化 · 控制台

一个窗口把三件事收在一起：
  1. 看状态   —— 余额读数、当前会走哪条流程、16 个标定区域的坐标
  2. 跑流程   —— 启动 / 停止自动点击，日志实时滚在下面
  3. 标定     —— 调起标定覆盖层，标完自动重读 config.json

设计上刻意克制：深色单窗口，一栏区域清单，一栏状态与操作，底部日志。
不弹多个窗口，不放花哨动效，所有信息一眼能扫完。

用法：
    python gui.py
"""
import os
import sys
import json
import time
import queue
import threading
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dvc_deps

dvc_deps.require(__file__, tk=True)

import tkinter as tk
from tkinter import messagebox

import dvc_win as W

W.ensure_dpi_aware()

import dvc_auto as A
import dvc_vision as V

# ---------------------------------------------------------------------------
# 配色与尺寸
# ---------------------------------------------------------------------------
BG      = "#11151b"     # 窗口底
PANEL   = "#171d25"     # 面板
PANEL2  = "#1d2530"     # 卡片 / 输入区
EDGE    = "#2a3441"     # 分隔线
EDGE2   = "#36414f"     # 悬停边框

TEXT    = "#dce5ef"
TEXT2   = "#93a2b4"
TEXT3   = "#61707f"

GREEN   = "#46e0a8"
GREEN_D = "#2ba87c"
AMBER   = "#f0b429"
RED     = "#f2555a"
RED_D   = "#c23b40"

BTN_BG   = "#242e3b"
BTN_HOV  = "#2f3c4c"


def _cfg_path():
    return A.resolve_config(None)


# 16 个标定区域的显示顺序与中文名（和标定程序里的编号一一对应）
REGION_ORDER = [
    ("btn_peizhuang", "主菜单 · 配装"),
    ("tab_cangku",    "顶部 · 仓库页签"),
    ("ocr_balance",   "右上 · 余额数字"),
    ("scan_blank",    "仓库 · 中间装备区"),
    ("btn_cart",      "仓库 · 购物车"),
    ("btn_sell",      "仓库 · 出售"),
    ("btn_sell_ok",   "出售弹窗 · 出售"),
    ("slot_helmet",   "配装 · 头盔槽"),
    ("slot_armor",    "配装 · 护甲槽"),
    ("drag_scroll",   "选择页 · 滚动条"),
    ("click_a",       "选择页 · 刷价点 A"),
    ("click_b",       "选择页 · 刷价点 B"),
    ("ocr_price",     "选择页 · 最低行价格"),
    ("btn_buy_1",     "选择页 · 购买 1"),
    ("btn_buy_2",     "选择页 · 购买 2"),
    ("click_center",  "兜底 · 屏幕中心"),
]


class App:
    def __init__(self, root):
        self.root = root
        self.cfg_path = _cfg_path()
        self.cfg = A.load_cfg(self.cfg_path)
        self.regions = self.cfg["regions"]

        s = W.screen_info()
        self.S = max(1.0, s["dpi"] / 96.0)
        self.scr = s

        self.grabber = W.Grabber()
        self.ocr = V.DigitOCR(A.DIGIT_DIR)
        self.balance = None
        self.balance_raw = ""

        self.proc = None            # 自动化子进程
        self.calib_proc = None      # 标定子进程
        self.q = queue.Queue()      # 后台线程 -> 主线程的唯一通道
        self._log_tail = 0
        self._running = False

        self._build()
        self._load_regions()
        self._dark_titlebar()
        self.log("准备就绪。先「读余额」确认识别，再「开始运行」。", "sys")
        self.root.after(400, self.refresh_balance)
        self.root.after(50, self._drain)

    def _drain(self):
        """主线程定时把后台线程塞进来的消息取出来处理。

        Tk 不允许在别的线程里碰控件，连 root.after() 都不行
        （Python 3.10+ 会直接抛 main thread is not in main loop），
        所以子进程的每一行输出都先进队列，在这里转交给界面。
        """
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == "log":
                self.log(item[1], item[2])
            elif kind == "exit":
                self._on_exit(item[1])
            elif kind == "calib":
                self._calib_done(item[1], item[2])
        try:
            self.root.after(50, self._drain)
        except tk.TclError:
            pass

    def _dark_titlebar(self):
        """让系统标题栏也变深色，不然一块浅灰压在深色界面上很跳。"""
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                return
            for attr in (20, 19):       # Win10 20H1+ 用 20，更早用 19
                val = ctypes.c_int(1)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                    break
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 尺寸换算
    # ------------------------------------------------------------------
    def px(self, v):
        return int(round(v * self.S))

    def font(self, size, bold=False, mono=False):
        name = "Consolas" if mono else "Microsoft YaHei UI"
        return (name, -self.px(size), "bold" if bold else "normal")

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------
    def _build(self):
        S, px = self.S, self.px
        self.root.title("三角洲自动化")
        self.root.configure(bg=BG)
        self.root.geometry("%dx%d+%d+%d" % (
            px(900), px(660),
            max(0, (self.scr["w"] - px(900)) // 2),
            max(0, (self.scr["h"] - px(660)) // 2 - px(40))))
        self.root.minsize(px(800), px(560))

        self._build_topbar()

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=px(14), pady=(px(10), px(14)))
        body.grid_columnconfigure(0, weight=0, minsize=px(340))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)
        self._build_left(left)

        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(px(14), 0))
        self._build_right(right)

    # ---------------- 顶栏 ----------------
    def _build_topbar(self):
        px, S = self.px, self.S
        bar = tk.Frame(self.root, bg=PANEL, height=px(50))
        bar.pack(fill="x")
        bar.pack_propagate(False)

        tk.Label(bar, text="三角洲自动化", bg=PANEL, fg=TEXT,
                 font=self.font(15, bold=True)).pack(side="left", padx=(px(16), 0))

        self.lbl_mode = tk.Label(bar, text="就绪", bg=PANEL, fg=TEXT3,
                                 font=self.font(11))
        self.lbl_mode.pack(side="left", padx=(px(12), 0))

        tk.Label(bar, text="F9 急停", bg=PANEL, fg=TEXT3,
                 font=self.font(10)).pack(side="right", padx=(0, px(16)))

        self.dot = tk.Canvas(bar, width=px(9), height=px(9), bg=PANEL,
                             highlightthickness=0)
        self.dot.pack(side="right", padx=(0, px(6)))
        self._dot_id = self.dot.create_oval(0, 0, px(9), px(9),
                                            fill=TEXT3, outline="")

        tk.Frame(self.root, bg=EDGE, height=1).pack(fill="x")

    # ---------------- 左：区域清单 ----------------
    def _build_left(self, parent):
        px = self.px
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", pady=(0, px(8)))
        tk.Label(head, text="标定区域", bg=BG, fg=TEXT2,
                 font=self.font(11, bold=True)).pack(side="left")
        self.lbl_cfg = tk.Label(head, text="", bg=BG, fg=TEXT3,
                                font=self.font(10))
        self.lbl_cfg.pack(side="right")

        wrap = tk.Frame(parent, bg=PANEL, highlightbackground=EDGE,
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True)

        self.list_cv = tk.Canvas(wrap, bg=PANEL, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(wrap, orient="vertical", command=self.list_cv.yview,
                          width=px(10), bg=PANEL, troughcolor=PANEL,
                          activebackground=EDGE2, relief="flat", bd=0,
                          highlightthickness=0, elementborderwidth=0)
        self.list_cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.list_cv.pack(side="left", fill="both", expand=True)
        self.list_cv.bind("<MouseWheel>",
                          lambda e: self.list_cv.yview_scroll(-1 * (e.delta // 120), "units"))
        self.list_cv.bind("<Configure>", self._on_list_resize)

        bottom = tk.Frame(parent, bg=BG)
        bottom.pack(fill="x", pady=(px(8), 0))
        self.btn_calib = self._button(bottom, "重新标定", self.on_calibrate, "ghost")
        self.btn_calib.pack(fill="x")

    # ---------------- 右：状态 + 操作 + 日志 ----------------
    def _build_right(self, parent):
        px = self.px

        # ----- 余额卡 -----
        card = tk.Frame(parent, bg=PANEL, highlightbackground=EDGE,
                        highlightthickness=1)
        card.pack(fill="x")

        top = tk.Frame(card, bg=PANEL)
        top.pack(fill="x", padx=px(16), pady=(px(12), 0))
        tk.Label(top, text="当前余额", bg=PANEL, fg=TEXT2,
                 font=self.font(11)).pack(side="left")
        self.lbl_branch = tk.Label(top, text="", bg=PANEL, fg=TEXT3,
                                   font=self.font(11))
        self.lbl_branch.pack(side="right")

        self.lbl_balance = tk.Label(card, text="···", bg=PANEL, fg=TEXT2,
                                    font=self.font(30, bold=True, mono=True),
                                    anchor="w")
        self.lbl_balance.pack(fill="x", padx=px(16), pady=(px(2), 0))

        self.lbl_hint = tk.Label(card, text="", bg=PANEL, fg=TEXT3,
                                 font=self.font(10), anchor="w")
        self.lbl_hint.pack(fill="x", padx=px(16), pady=(0, px(12)))

        # ----- 操作行 -----
        ops = tk.Frame(parent, bg=BG)
        ops.pack(fill="x", pady=(px(12), 0))

        self.btn_start = self._button(ops, "开始运行", self.on_start, "primary")
        self.btn_start.pack(side="left", ipady=px(6), ipadx=px(14))
        self.btn_stop = self._button(ops, "停止", self.on_stop, "danger")
        self.btn_stop.pack(side="left", padx=(px(8), 0), ipady=px(6), ipadx=px(14))
        self.btn_stop.configure(state="disabled", bg=BTN_BG, fg=TEXT3)
        self.btn_stop._bg = BTN_BG

        self.btn_probe = self._button(ops, "读余额", self.refresh_balance, "ghost")
        self.btn_probe.pack(side="right", ipady=px(6), ipadx=px(10))

        # ----- 日志 -----
        logwrap = tk.Frame(parent, bg=PANEL, highlightbackground=EDGE,
                           highlightthickness=1)
        logwrap.pack(fill="both", expand=True, pady=(px(12), 0))

        head = tk.Frame(logwrap, bg=PANEL, height=px(28))
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="运行日志", bg=PANEL, fg=TEXT2,
                 font=self.font(10, bold=True)).pack(side="left", padx=px(12))

        self.log_txt = tk.Text(logwrap, bg=PANEL, fg=TEXT, bd=0,
                               highlightthickness=0, wrap="word",
                               font=self.font(10, mono=True),
                               padx=px(12), pady=px(6),
                               insertbackground=TEXT, state="disabled")
        lsb = tk.Scrollbar(logwrap, orient="vertical", command=self.log_txt.yview,
                           width=px(10), bg=PANEL, troughcolor=PANEL,
                           activebackground=EDGE2, relief="flat", bd=0,
                           highlightthickness=0, elementborderwidth=0)
        self.log_txt.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.log_txt.pack(side="left", fill="both", expand=True)

        self.log_txt.tag_configure("info", foreground=TEXT2)
        self.log_txt.tag_configure("act", foreground=TEXT)
        self.log_txt.tag_configure("step", foreground=GREEN)
        self.log_txt.tag_configure("warn", foreground=AMBER)
        self.log_txt.tag_configure("fatal", foreground=RED)
        self.log_txt.tag_configure("sys", foreground=TEXT3)

    def _button(self, parent, text, cmd, kind="normal"):
        px = self.px
        styles = {
            "normal":  (BTN_BG, TEXT, BTN_HOV),
            "ghost":   (PANEL2, TEXT2, BTN_HOV),
            "primary": (GREEN, "#0b1a14", GREEN_D),
            "danger":  (RED, "#1a0b0c", RED_D),
        }
        bg, fg, hov = styles.get(kind, styles["normal"])
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=hov, activeforeground=fg,
                      relief="flat", bd=0, highlightthickness=0,
                      cursor="hand2", font=self.font(11, bold=(kind == "primary")),
                      padx=px(10), pady=px(4))
        b.bind("<Enter>", lambda e: b.configure(bg=hov) if str(b["state"]) != "disabled" else None)
        b.bind("<Leave>", lambda e: b.configure(bg=bg) if str(b["state"]) != "disabled" else None)
        b._bg, b._fg, b._hov = bg, fg, hov
        return b

    # ------------------------------------------------------------------
    # 区域清单
    # ------------------------------------------------------------------
    def _load_regions(self):
        self.regions = self.cfg["regions"]
        self.idx_of_key = {}
        self._draw_list()
        name = os.path.basename(self.cfg_path)
        scr = self.cfg.get("screen") or {}
        self.lbl_cfg.configure(text="%s  %sx%s" % (
            name, scr.get("w", "?"), scr.get("h", "?")))

    def _draw_list(self):
        S, px = self.S, self.px
        cv = self.list_cv
        cv.delete("all")
        self.row_h = px(32)
        sw, sh = self.scr["w"], self.scr["h"]
        cw = getattr(self, "_last_cv_w", 0) or px(340)
        right = cw - px(16)

        y = 0
        for i, (key, name) in enumerate(REGION_ORDER):
            if key not in self.regions:
                continue
            self.idx_of_key[key] = y // self.row_h
            rect = cv.create_rectangle(0, y, px(400), y + self.row_h,
                                       fill=PANEL, outline="", tags=("row", key))
            cv.create_line(px(12), y + self.row_h - 1, cw - px(10),
                           y + self.row_h - 1, fill="#202934", tags=("row", key))

            ok = self._in_screen(self.regions[key], sw, sh)
            color = GREEN if ok else RED
            cy = y + self.row_h // 2
            cv.create_oval(px(14), cy - px(3), px(14) + px(6), cy + px(3),
                           fill=color, outline="", tags=("row", key))

            cv.create_text(px(30), cy, anchor="w", tags=("row", key),
                           text="%d  %s" % (i + 1, name),
                           fill=TEXT if ok else RED, font=self.font(11))

            coord = self._coord_text(self.regions[key])
            cv.create_text(right, cy, anchor="e", tags=("row", key),
                           text=coord, fill=TEXT3, font=self.font(9, mono=True))
            y += self.row_h

        cv.configure(scrollregion=(0, 0, px(340), y))
        cv.bind("<Button-1>", self._on_list_click)

    def _on_list_resize(self, ev):
        if abs(ev.width - getattr(self, "_last_cv_w", 0)) <= 2:
            return
        self._last_cv_w = ev.width
        self._draw_list()

    def _in_screen(self, v, sw, sh):
        if isinstance(v, list) and v and isinstance(v[0], (list, tuple)):
            pts = v
        elif isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
            pts = [v]
        else:
            pts = [v]
        for p in pts:
            if not (0 <= p[0] <= sw and 0 <= p[1] <= sh):
                return False
        return True

    def _coord_text(self, v):
        """列表里只显示起点：整串 4 个数字会把这一栏挤爆，完整值点行看日志。"""
        try:
            if isinstance(v, list) and v and isinstance(v[0], (list, tuple)):
                return "%d,%d ⇢" % (v[0][0], v[0][1])
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
                return "%d, %d" % (v[0], v[1])
            if isinstance(v, list) and len(v) == 4:
                return "%d,%d" % (v[0], v[1])
        except Exception:
            pass
        return str(v)

    def _coord_full(self, v):
        try:
            if isinstance(v, list) and v and isinstance(v[0], (list, tuple)):
                return "%d,%d → %d,%d" % (v[0][0], v[0][1], v[1][0], v[1][1])
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
                return "%d, %d" % (v[0], v[1])
            if isinstance(v, list) and len(v) == 4:
                return "%d,%d  %d×%d" % (v[0], v[1], v[2] - v[0], v[3] - v[1])
        except Exception:
            pass
        return str(v)

    def _on_list_click(self, ev):
        y = self.list_cv.canvasy(ev.y)
        row = int(y // self.row_h)
        for key, i in self.idx_of_key.items():
            if i == row:
                self.log("[区域] %s：%s" % (self._name_of(key), self._coord_full(self.regions[key])), "sys")
                break

    def _name_of(self, key):
        for k, n in REGION_ORDER:
            if k == key:
                return n
        return key

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def log(self, text, tag="info"):
        t = self.log_txt
        t.configure(state="normal")
        t.insert("end", time.strftime("%H:%M:%S ") + text + "\n", tag)
        t.see("end")
        t.configure(state="disabled")

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _set_dot(self, color):
        px = self.px
        self.dot.itemconfigure(self._dot_id, fill=color)

    def _set_running(self, on):
        self._running = on
        if on:
            self._set_dot(GREEN)
            self.lbl_mode.configure(text="运行中", fg=GREEN)
            self.btn_start.configure(state="disabled", bg=BTN_BG, fg=TEXT3)
            self.btn_start._bg = BTN_BG
            self.btn_stop.configure(state="normal", bg=RED, fg="#1a0b0c")
            self.btn_stop._bg = RED
            self.btn_calib.configure(state="disabled", bg=BTN_BG, fg=TEXT3)
            self.btn_calib._bg = BTN_BG
        else:
            self._set_dot(TEXT3)
            self.lbl_mode.configure(text="就绪", fg=TEXT3)
            self.btn_start.configure(state="normal", bg=GREEN, fg="#0b1a14")
            self.btn_start._bg = GREEN
            self.btn_stop.configure(state="disabled", bg=BTN_BG, fg=TEXT3)
            self.btn_stop._bg = BTN_BG
            self.btn_calib.configure(state="normal", bg=PANEL2, fg=TEXT2)
            self.btn_calib._bg = PANEL2

    # ------------------------------------------------------------------
    # 读余额
    # ------------------------------------------------------------------
    def refresh_balance(self):
        if self._running:
            return
        try:
            img = self.grabber.grab_box(A.box_of(self.regions, "ocr_balance"))
            r = self.ocr.read(img)
        except Exception as e:
            self.lbl_balance.configure(text="读取失败", fg=RED)
            self.lbl_hint.configure(text=str(e))
            return

        txt = (r.get("text") or "").upper()
        self.lbl_balance.configure(text=txt or "···",
                                   fg=TEXT if r.get("ok") else AMBER)
        self.lbl_hint.configure(
            text="原始读数 %r   置信 %.2f   %d 段" % (txt or "空", r.get("conf", 0.0), r.get("n", 0)))

        opts = self.cfg.get("options") or {}
        lo = int(opts.get("balance_low", 300000))
        hi = int(opts.get("balance_high", 800000))
        v = r.get("value")

        if "K" in txt or "W" in txt or "M" in txt:
            self.lbl_branch.configure(text="带单位 · 会暂停", fg=RED)
        elif v is None:
            self.lbl_branch.configure(text="读不出来", fg=AMBER)
        elif v < lo:
            self.lbl_branch.configure(text="走 A · 卖货补余额", fg=AMBER)
        elif v <= hi:
            self.lbl_branch.configure(text="走 B · 刷价抢购", fg=GREEN)
        else:
            self.lbl_branch.configure(text="超过 %d · 会暂停" % hi, fg=RED)

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------
    def on_start(self):
        if self._running:
            return
        ok = messagebox.askokcancel(
            "开始运行",
            "将开始真实点击。\n\n"
            "确认游戏已切到前台、并停在能看见余额的界面。\n"
            "运行中随时可以点「停止」，或按 F9 急停。",
            icon="warning", parent=self.root)
        if not ok:
            return
        self.log("启动自动化（真实点击）", "step")
        self._set_running(True)
        self._tail = threading.Thread(target=self._pump, daemon=True)
        self._tail.start()

    def _pump(self):
        py = sys.executable
        extra = os.environ.get("GUI_ARGS", "").split()
        cmd = [py, os.path.join(HERE, "dvc_auto.py")] + (extra or ["--live"])
        self.q.put(("log", "执行：dvc_auto.py " + " ".join(cmd[2:]), "sys"))
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=HERE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, encoding="utf-8",
                errors="replace", bufsize=1,
                creationflags=0x08000000)
        except Exception as e:
            self.q.put(("log", "启动失败：%s" % e, "fatal"))
            self.q.put(("exit", -1))
            return

        try:
            for line in self.proc.stdout:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                body, tag = self._parse_line(line)
                self.q.put(("log", body, tag))
        except Exception:
            pass

        try:
            code = self.proc.wait()
        except Exception:
            code = -1
        self.proc = None
        self.q.put(("exit", code))

    @staticmethod
    def _parse_line(line):
        body = line
        if line.startswith("[") and "]" in line:
            body = line.split("]", 1)[1].strip()
        level = ""
        for lv in ("STEP", "WARN", "FATAL", "INFO", "ACT"):
            if body.startswith(lv):
                level = lv
                body = body[len(lv):].strip()
                break
        tag = {"STEP": "step", "WARN": "warn", "FATAL": "fatal",
               "INFO": "info", "ACT": "act"}.get(level, "act")
        if "Traceback" in body or "Error:" in body:
            tag = "fatal"
        return body, tag

    def _on_exit(self, code):
        self._set_running(False)
        if code == 0:
            self.log("自动化结束", "sys")
        else:
            self.log("自动化退出，返回码 %d" % code, "warn")
        self.root.after(600, self.refresh_balance)

    def on_stop(self):
        if self.proc is None:
            return
        self.log("正在停止…", "warn")
        try:
            self.proc.terminate()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 标定
    # ------------------------------------------------------------------
    def on_calibrate(self):
        if self._running:
            return
        calib = os.path.join(os.path.dirname(HERE), "src", "标定程序", "dvc_calibrate.py")
        if not os.path.exists(calib):
            messagebox.showerror("找不到标定程序", calib, parent=self.root)
            return
        self.log("启动标定覆盖层，拖完框会自动回到这里", "step")
        self.root.withdraw()
        threading.Thread(target=self._run_calib, args=(calib,), daemon=True).start()

    def _run_calib(self, calib):
        # 不能用退出码判断存没存：标定程序的 __main__ 只调 run() 不 sys.exit，
        # 所以「放弃」和「保存」退出码都是 0。用 config.json 的 mtime 才准。
        try:
            before = os.path.getmtime(self.cfg_path)
        except OSError:
            before = 0.0
        try:
            self.calib_proc = subprocess.Popen(
                [sys.executable, calib], cwd=os.path.dirname(calib),
                creationflags=0x08000000)
            self.calib_proc.wait()
        except Exception as e:
            self.calib_proc = None
            self.q.put(("calib", False, str(e)))
            return
        self.calib_proc = None
        try:
            after = os.path.getmtime(self.cfg_path)
        except OSError:
            after = before
        self.q.put(("calib", after != before, ""))

    def _calib_done(self, saved, err):
        self.root.deiconify()
        self.root.lift()
        if err:
            self.log("标定程序没能启动：%s" % err, "fatal")
            return
        if not saved:
            self.log("标定已放弃，config.json 没有改动", "warn")
            return
        try:
            self.cfg = A.load_cfg(self.cfg_path)
            self.regions = self.cfg["regions"]
            self._load_regions()
            self.log("标定已保存，重新读取 %d 个区域" % len(self.regions), "step")
        except Exception as e:
            self.log("重读 config 失败：%s" % e, "fatal")
        self.refresh_balance()


def main():
    root = tk.Tk()
    try:
        App(root)
    except Exception as e:
        import traceback
        traceback.print_exc()
        root.withdraw()
        messagebox.showerror("启动失败", "%s\n\n%s" % (e, traceback.format_exc(limit=2)))
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
