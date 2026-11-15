# -*- coding: utf-8 -*-
r"""
OCR 验证工具：鼠标拖框选一块区域，实时显示识别结果和速度。

用法：
    python ocr_probe.py                      拖框交互
    python ocr_probe.py --box 2468,168,2610,209   跳过拖框，直接盯这个区域
    python ocr_probe.py --png 截图.png --once      拿一张图测一次就退

快捷键：
    F9 重新框选      F8 把当前帧存到 debug\      ESC 退出
"""
import argparse
import ctypes
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import dvc_deps

dvc_deps.require(__file__, tk=True)

import tkinter as tk
from tkinter import font as tkfont

import dvc_win as W
import dvc_vision as V

DIGITS = os.path.join(BASE, "digits")
DEBUG = os.path.join(BASE, "debug")

TRANSPARENT = "#0a0a0a"
ACCENT = "#3ee6a8"
CARD_BG = "#10151b"
CARD_EDGE = "#2c3d4c"
FG = "#e8eef5"
DIM = "#8fa3b0"
WARN = "#ffc85c"
BAD = "#ff6b6b"

VK_F8 = 0x77
VK_F9 = 0x78
VK_ESC = 0x1B

UNIT_CHARS = ("K", "W", "M", "亿", "万")


class Probe:
    def __init__(self, box=None, png=None, once=False):
        W.ensure_dpi_aware()
        self.scr = W.screen_info()
        self.grabber = W.Grabber()
        self.ocr = V.DigitOCR(DIGITS)
        self.png = png
        self.once = once
        self.box = box
        self.running = False
        self.overlay = None
        self.hud = None

        self.ocr_ms = []
        self.grab_ms = []
        self.stamps = []
        self.total = 0
        self.last_save = 0.0
        self.last_print = 0.0
        self.last_hud = 0.0

        self.hud_cv = None
        self.last_img = None

        os.makedirs(DEBUG, exist_ok=True)

        if png:
            self.run_png(png)
            return

        self.root = tk.Tk()
        self.root.withdraw()
        self.f_num = tkfont.Font(family="Consolas", size=-38, weight="bold")
        self.f_mid = tkfont.Font(family="Microsoft YaHei", size=-15)
        self.f_small = tkfont.Font(family="Microsoft YaHei", size=-13)
        self.f_tiny = tkfont.Font(family="Microsoft YaHei", size=-12)

        if box:
            self.start(box)
        else:
            self.ask_box()
        self.root.mainloop()

    # ------------------------------------------------------------------
    # 选区域
    # ------------------------------------------------------------------
    def ask_box(self):
        self.teardown()
        self.running = False
        s = self.scr
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.geometry("%dx%d+%d+%d" % (s["vw"], s["vh"], s["vx"], s["vy"]))
        # Tk 的 geometry 在 200% 缩放下不靠谱（跨进程实测只拿到半屏），
        # 建完之后再用 Win32 把物理矩形钉死一次。
        top.update_idletasks()
        try:
            hwnd = W.u32.GetAncestor(top.winfo_id(), 2)
            if hwnd:
                W.u32.SetWindowPos.restype = W.wintypes.BOOL
                W.u32.SetWindowPos.argtypes = [
                    W.wintypes.HWND, W.wintypes.HWND,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.c_uint,
                ]
                W.u32.SetWindowPos(W.wintypes.HWND(hwnd), W.wintypes.HWND(-1),
                                   int(s["vx"]), int(s["vy"]),
                                   int(s["vw"]), int(s["vh"]), 0x0040)
        except Exception as exc:                      # noqa: BLE001
            print("强制全屏失败（不影响使用，只是窗口可能不满屏）: %s" % exc)
        # 这里不能用 -transparentcolor。COLORKEY 抠掉的像素不只看不见，
        # 连鼠标消息都不接收，结果就是「只有画了东西的那一小块能点，其他地方
        # 全都穿透到桌面」。改成整窗半透明遮罩，整块屏幕都能拖。
        try:
            top.attributes("-alpha", 0.42)
        except tk.TclError:
            pass
        cv = tk.Canvas(top, bg="#05080b", highlightthickness=0, cursor="cross")
        cv.pack(fill="both", expand=True)
        # 提示条画在实心底色块上：透明色窗口里直接画字，抗锯齿边缘会混进
        # TRANSPARENT 被一起抠掉，字就成毛边花字了。
        cx = s["vw"] // 2
        cv.create_rectangle(cx - 320, 10, cx + 320, 96,
                            fill="#0e141a", outline="#3ee6a8", width=2)
        cv.create_text(cx, 36, text="拖拽框选要识别的数字区域",
                       fill="#7dffd0", font=self.f_mid)
        cv.create_text(cx, 70, text="松开开始　F9 重选　ESC 退出",
                       fill="#c9d6e2", font=self.f_small)

        st = {"x": 0, "y": 0, "on": False}

        def press(e):
            st["on"] = True
            st["x"], st["y"] = e.x, e.y
            cv.delete("sel")

        def move(e):
            if not st["on"]:
                return
            self._draw_sel(cv, st["x"], st["y"], e.x, e.y)

        def release(e):
            if not st["on"]:
                return
            st["on"] = False
            x0, x1 = sorted((st["x"], e.x))
            y0, y1 = sorted((st["y"], e.y))
            if x1 - x0 < 8 or y1 - y0 < 8:
                cv.delete("sel")
                return
            box = (x0 + s["vx"], y0 + s["vy"], x1 + s["vx"], y1 + s["vy"])
            self.start(box)

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", move)
        cv.bind("<ButtonRelease-1>", release)
        self.overlay = top
        self.root.after(60, self._wait_hotkey)

    def _draw_sel(self, cv, x0, y0, x1, y1):
        cv.delete("sel")
        cv.create_rectangle(x0, y0, x1, y1, outline=ACCENT, width=3, tags="sel")
        w, h = abs(x1 - x0), abs(y1 - y0)
        tx = min(x0, x1)
        ty = min(y0, y1) - 26
        if ty < 6:
            ty = max(y0, y1) + 6
        cv.create_text(tx + 2, ty, text="%d x %d" % (w, h), anchor="nw",
                       fill=ACCENT, font=self.f_small, tags="sel")

    # ------------------------------------------------------------------
    # 开始识别
    # ------------------------------------------------------------------
    def start(self, box):
        x0, y0, x1, y1 = [int(v) for v in box]
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            print("区域太小了")
            return
        self.box = box
        self.teardown()
        self.ocr_ms.clear()
        self.grab_ms.clear()
        self.stamps.clear()
        self.total = 0
        self.last_hud = 0.0
        try:
            self.ocr.read(self.grabber.grab_box(box))      # 热身，首帧有 ~18ms 冷启动开销
        except Exception:                                  # noqa: BLE001
            pass
        self.make_hud()
        self.running = True
        print("区域 %dx%d @ (%d,%d)  模板 %d 类 / %d 张"
              % (box[2] - box[0], box[3] - box[1], box[0], box[1],
                 len(self.ocr.model), sum(len(v) for v in self.ocr.model.values())))
        self.root.after(30, self.tick)

    def run_png(self, path):
        from PIL import Image
        import numpy as np
        rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
        try:
            segs = self.ocr.segment(rgb)
        except Exception as e:                      # noqa: BLE001
            segs = []
            print("切分失败：%r" % (e,))
        r = self.ocr.read(rgb)
        t0 = time.perf_counter()
        for _ in range(50):
            r = self.ocr.read(rgb)
        dt = (time.perf_counter() - t0) / 50 * 1000
        print("图 %s  %dx%d  切出 %d 段"
              % (os.path.basename(path), rgb.shape[1], rgb.shape[0], len(segs)))
        print("识别 = %r   value=%s   conf=%.3f   耗时 %.2f ms"
              % (r["text"], r["value"], r["conf"], dt))
        if segs:
            print("段宽 " + " ".join("%d" % (s[1] - s[0]) for s in segs))
            print("段高 " + " ".join("%d" % (s[3] - s[2]) for s in segs))

    # ------------------------------------------------------------------
    # HUD
    # ------------------------------------------------------------------
    def make_hud(self):
        x0, y0, x1, y1 = self.box
        hud_w, hud_h = 372, 148
        s = self.scr
        hx = min(max(x0, 10), max(10, s["w"] - hud_w - 10))
        hy = y1 + 12
        if hy + hud_h > s["h"] - 10:
            hy = y0 - hud_h - 12
        if hy < 10:
            hy = 10
        hud = tk.Toplevel(self.root)
        hud.overrideredirect(True)
        hud.attributes("-topmost", True)
        if os.environ.get("PROBE_SOLID") != "1":
            hud.attributes("-alpha", 0.94)
        hud.geometry("%dx%d+%d+%d" % (hud_w, hud_h, hx, hy))
        cv = tk.Canvas(hud, bg=CARD_BG, highlightthickness=0,
                       width=hud_w, height=hud_h)
        cv.pack(fill="both", expand=True)
        cv.create_rectangle(0, 0, hud_w - 1, hud_h - 1, outline=CARD_EDGE, width=2)
        cv.create_rectangle(0, 0, 0, 0)          # 占位，保证 delete 后边框还在
        self.hud = hud
        self.hud_cv = cv
        self.hud_w, self.hud_h = hud_w, hud_h
        self._hud_frame_static()
        hud.update_idletasks()

    def _hud_frame_static(self):
        cv = self.hud_cv
        cv.delete("dyn")
        cv.create_rectangle(0, 0, self.hud_w - 1, self.hud_h - 1,
                            outline=CARD_EDGE, width=2, tags="dyn")
        cv.create_line(12, 92, self.hud_w - 12, 92, fill="#1e2a35", tags="dyn")

    def update_hud(self, res, grab_ms, ocr_ms, fps):
        cv = self.hud_cv
        x0, y0, x1, y1 = self.box
        cv.delete("dyn")
        cv.create_rectangle(0, 0, self.hud_w - 1, self.hud_h - 1,
                            outline=CARD_EDGE, width=2, tags="dyn")

        txt = res["text"] or "(空)"
        if any(u in txt.upper() for u in UNIT_CHARS):
            color = WARN
        elif res["ok"]:
            color = ACCENT
        else:
            color = BAD
        cv.create_text(14, 6, text=txt, anchor="nw", fill=color,
                       font=self.f_num, tags="dyn")

        avg = sum(self.ocr_ms) / len(self.ocr_ms) if self.ocr_ms else 0.0
        peak = max(self.ocr_ms) if self.ocr_ms else 0.0
        cv.create_text(14, 62, anchor="nw", fill=FG, font=self.f_small,
                       text="识别 %.2f ms    均 %.2f    峰 %.2f" % (ocr_ms, avg, peak),
                       tags="dyn")
        cv.create_text(self.hud_w - 14, 62, anchor="ne", fill=ACCENT,
                       font=self.f_mid, text="%d 次/秒" % fps, tags="dyn")
        cv.create_text(14, 92 + 8, anchor="nw", fill=DIM, font=self.f_tiny,
                       text="抓图 %.2f ms   区域 %dx%d   字 %d   置信 %.2f"
                       % (grab_ms, x1 - x0, y1 - y0, res.get("n", 0), res["conf"]),
                       tags="dyn")
        cv.create_text(14, self.hud_h - 20, anchor="nw", fill="#5d7180",
                       font=self.f_tiny,
                       text="F9 重选   F8 存图   ESC 退出", tags="dyn")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def tick(self):
        if not self.running:
            return
        if W.key_down(VK_ESC):
            self.quit()
            return
        if W.key_down(VK_F9):
            self.ask_box()
            return
        if W.key_down(VK_F8) and time.perf_counter() - self.last_save > 0.4:
            self.last_save = time.perf_counter()
            self.dump_frame()

        t0 = time.perf_counter()
        try:
            img = self.grabber.grab_box(self.box)
        except Exception as e:                      # noqa: BLE001
            self.log("抓图失败：%r" % (e,))
            self.root.after(200, self.tick)
            return
        t1 = time.perf_counter()
        try:
            res = self.ocr.read(img)
        except Exception as e:                      # noqa: BLE001
            self.log("识别失败：%r" % (e,))
            self.root.after(200, self.tick)
            return
        t2 = time.perf_counter()

        grab_ms = (t1 - t0) * 1000.0
        ocr_ms = (t2 - t1) * 1000.0
        self.last_img = img
        self.total += 1

        if self.total > 1:                                 # 第 1 帧含着色器/内存池冷启动，不计入均值
            self.ocr_ms.append(ocr_ms)
            self.grab_ms.append(grab_ms)
            if len(self.ocr_ms) > 150:
                self.ocr_ms.pop(0)
                self.grab_ms.pop(0)

        now = time.perf_counter()
        self.stamps.append(now)
        while self.stamps and now - self.stamps[0] > 1.0:
            self.stamps.pop(0)
        fps = len(self.stamps)

        # HUD 单独节流到 ~14 帧/秒，并在每次重画后强制刷新 idle 队列。
        # 采样循环是 after(1) 的紧密循环，如果不 update_idletasks，Canvas 的
        # 重绘（idle 任务）会被饿死，窗口只剩背景色。
        if now - self.last_hud > 0.07:
            self.last_hud = now
            self.update_hud(res, grab_ms, ocr_ms, fps)
            if self.hud is not None:
                self.hud.update_idletasks()

        if now - self.last_print > 0.5:
            self.last_print = now
            print("[%s] %-10r 识别 %5.2f ms  抓图 %5.2f ms  %3d 次/秒  conf %.2f"
                  % (time.strftime("%H:%M:%S"), res["text"], ocr_ms, grab_ms,
                     fps, res["conf"]), flush=True)

        if self.once:
            self.running = False
            self.dump_frame()
            self.quit()
            return

        self.root.after(1, self.tick)

    def dump_frame(self):
        img = getattr(self, "last_img", None)
        if img is None:
            self.log("还没有帧可存")
            return
        p = os.path.join(DEBUG, "probe_%s.png" % time.strftime("%H%M%S"))
        V.save_rgb(p, img)
        self.log("已存 %s" % p)
        if self.hud_cv is not None and self.hud is not None:
            self.hud_cv.create_text(self.hud_w - 14, self.hud_h - 20, anchor="ne",
                                    fill=ACCENT, font=self.f_tiny,
                                    text="已存图", tags="saveflash")
            self.root.after(700, self._clear_save_flash)

    def _clear_save_flash(self):
        if self.hud_cv is None:
            return
        try:
            self.hud_cv.delete("saveflash")
        except tk.TclError:
            pass

    def _wait_hotkey(self):
        if self.overlay is None:
            return
        if W.key_down(VK_ESC):
            self.quit()
            return
        self.root.after(60, self._wait_hotkey)

    # ------------------------------------------------------------------
    def teardown(self):
        for w in (self.overlay, self.hud):
            if w is not None:
                try:
                    w.destroy()
                except tk.TclError:
                    pass
        self.overlay = None
        self.hud = None
        self.hud_cv = None

    def quit(self):
        self.running = False
        self.teardown()
        try:
            self.grabber.close()
        except Exception:                           # noqa: BLE001
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass

    @staticmethod
    def log(msg):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def parse_box(s):
    parts = [p.strip() for p in str(s).replace("，", ",").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--box 需要 x1,y1,x2,y2 四个整数")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError("--box 里只能放整数")


def main():
    ap = argparse.ArgumentParser(description="OCR 验证：拖框 -> 实时识别 -> 看速度和结果")
    ap.add_argument("--box", type=parse_box, metavar="X1,Y1,X2,Y2",
                    help="跳过拖框，直接盯这个区域")
    ap.add_argument("--png", metavar="PATH", help="直接从一张图片识别（用于自测）")
    ap.add_argument("--region", metavar="KEY",
                    help="直接用 config.json 里的区域名当框，例如 ocr_balance / ocr_price")
    ap.add_argument("--once", action="store_true", help="只识别一次就退出")
    args = ap.parse_args()
    if args.png and not os.path.isfile(args.png):
        print("找不到图片：%s" % args.png)
        return 2
    if args.region and not args.png:
        try:
            import dvc_auto as A
            cfg = A.load_cfg(A.resolve_config(None))
            if args.region not in (cfg.get("regions") or {}):
                print("config.json 里没有区域 %r，可用：%s"
                      % (args.region, " ".join(sorted(cfg.get("regions") or {}))))
                return 2
            args.box = tuple(A.box_of(cfg["regions"], args.region))
        except Exception as e:                      # noqa: BLE001
            print("读区域失败：%r" % (e,))
            return 2
    Probe(box=args.box, png=args.png, once=args.once)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
