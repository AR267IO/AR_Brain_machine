# -*- coding: utf-8 -*-
r"""
初始化标定向导
---------------
两层结构：
  * 底层：全屏透明覆盖层，只负责画框（鼠标完全穿透，不影响你操作游戏）
  * 上层：一个小面板（背景透明），放提示文字和按钮，可以拖到不挡事的地方

分辨率 / 缩放适配：
  * 进程声明 Per-Monitor V2 DPI 感知，任何缩放下坐标都是物理像素，不会偏一半
  * 覆盖层铺满虚拟桌面（多显示器也覆盖得到）
  * 面板尺寸按屏幕大小和 DPI 自适应，小屏幕不会溢出，也不会盖住按钮区
  * 读 config.json 时如果发现标定时的分辨率和现在不一样，自动等比换算，
    并在面板上明确提示「位置可能不准，建议重标」

操作：
    Ctrl + 左键 拖 / 点    标定当前项
    Ctrl + 右键            上一项
    F8                     当前项恢复成默认位置
    面板按钮「上一项 / 跳过 / 保存并结束」
    Ctrl + Alt + Q         放弃退出（不保存）

ESC 不绑定任何功能（游戏里要用）。
"""
import ctypes
import sys
import threading
import time
import queue
from ctypes import wintypes

import dvc_config as C

u = ctypes.windll.user32
u.CallNextHookEx.restype = ctypes.c_ssize_t
u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t]
u.GetWindowLongW.restype = ctypes.c_long
u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u.SetWindowLongW.restype = ctypes.c_long
u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
u.GetAsyncKeyState.restype = ctypes.c_short
u.GetAsyncKeyState.argtypes = [ctypes.c_int]
u.GetParent.restype = ctypes.c_void_p
u.GetParent.argtypes = [ctypes.c_void_p]
u.SetWindowPos.restype = ctypes.c_bool
u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, ctypes.c_uint]

# DPI 必须在建任何窗口之前声明，且只能声明一次
DPI_MODE = C.ensure_dpi_aware()
SCREEN = C.screen_info()
DPI = SCREEN["dpi"]
SCREEN_W, SCREEN_H = SCREEN["w"], SCREEN["h"]        # 主屏物理像素（坐标基准）
VIEW_X, VIEW_Y = SCREEN["vx"], SCREEN["vy"]          # 虚拟桌面原点（可能为负，多显示器）
VIEW_W, VIEW_H = SCREEN["vw"], SCREEN["vh"]          # 虚拟桌面尺寸（覆盖层铺这个）
S = DPI / 96.0
UI_K = min(S, 2.5)     # 面板排版的缩放上限：DPI 再高也不让控件尺寸无限膨胀


def px(v):
    """逻辑像素 -> 当前 DPI 下的物理像素（画框线宽、标记点用）"""
    return max(1, int(round(v * S)))


def pxu(v):
    """逻辑像素 -> 面板 UI 尺寸（带上限，超高 DPI 小屏也不至于撑爆）"""
    return max(1, int(round(v * UI_K)))


def panel_size(sw=None, sh=None, dpi=None, notice=False):
    """
    面板尺寸（纯函数，方便跨分辨率测试）。
    宽度不超过屏幕 55%、高度不超过 45%，小屏 / 高缩放组合下也不会溢出屏幕。
    """
    sw = sw or SCREEN_W
    sh = sh or SCREEN_H
    k = min((dpi or DPI) / 96.0, 2.5)

    def p(v):
        return max(1, int(round(v * k)))

    base_h = 252 if notice else 220
    pw = max(min(p(560), int(sw * 0.55)), min(p(300), int(sw * 0.50)))
    # 高度上限给到 62%：1366x768@200% 这类「小屏 + 高缩放」组合，
    # 提示 + 确认文本会换很多行，0.45 的上限会把按钮裁掉。
    ph = max(min(p(base_h), int(sh * 0.62)), min(p(170), int(sh * 0.40)))
    return min(pw, sw), min(ph, sh)


def to_view(x, y):
    """屏幕坐标 -> 覆盖层 canvas 坐标（多显示器时原点不在左上角）"""
    return x - VIEW_X, y - VIEW_Y


def to_view_box(b):
    """矩形 [x1,y1,x2,y2] -> 覆盖层 canvas 坐标"""
    x1, y1, x2, y2 = b
    return (x1 - VIEW_X, y1 - VIEW_Y, x2 - VIEW_X, y2 - VIEW_Y)


KEY = "#0a0a0a"
GREEN = "#2ee6a8"
YELLOW = "#ffd24a"
CYAN = "#39d6ff"
WHITE = "#ffffff"
PINK = "#ff5a9e"
GREY = "#8fa3b0"
RED = "#ff5a5a"
BTN_BG = "#1d2b36"

WH_MOUSE_LL, WH_KEYBOARD_LL = 14, 13
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN = 0x0201, 0x0202, 0x0204
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
VK_CONTROL, VK_MENU, VK_Q, VK_F8, VK_F9, VK_BACK = 0x11, 0x12, 0x51, 0x77, 0x78, 0x08
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
HWND_TOPMOST, SWP_NOACTIVATE = ctypes.c_void_p(-1), 0x0010


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


# 必须用本地 POINT 类声明：wintypes.POINT 是另一个类型对象，
# 参数类型不匹配时 ctypes 会抛 ArgumentError（曾导致按下鼠标后 poll 链断掉）
u.GetCursorPos.restype = wintypes.BOOL
u.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t)


def key_down(vk):
    return bool(u.GetAsyncKeyState(vk) & 0x8000)



class _Hook:
    """鼠标钩子负责抓 Ctrl+左键/右键。

    Ctrl 是否按下，除了 GetAsyncKeyState 之外还额外用一个键盘钩子跟踪：
    GetAsyncKeyState 在进程不在前台时不一定可靠（文档明确不建议用它当按键
    状态源），而钩子给出的 down/up 是准的。两个都用，任意一个为真就算按下。
    """

    def __init__(self, q):
        self.q = q
        self.dragging = False
        self.ctrl = False
        self.arm = False
        self._proc = HOOKPROC(self._cb)
        self._kproc = HOOKPROC(self._kcb)

    def _kcb(self, ncode, wparam, lparam):
        try:
            if ncode == 0:
                vk = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
                if vk in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL):
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        self.ctrl = True
                    elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                        self.ctrl = False
        except Exception:
            pass
        return u.CallNextHookEx(0, ncode, wparam, lparam)

    def ctrl_down(self):
        return self.ctrl or key_down(VK_CONTROL)

    def _cb(self, ncode, wparam, lparam):
        try:
            if ncode == 0:
                info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                x, y = int(info.pt.x), int(info.pt.y)
                if self.ctrl_down() or self.arm:
                    if wparam == WM_LBUTTONDOWN:
                        self.dragging = True
                        self.q.put(("down", x, y))
                        return 1
                    if wparam == WM_LBUTTONUP and self.dragging:
                        self.dragging = False
                        self.q.put(("up", x, y))
                        return 1
                    if wparam == WM_RBUTTONDOWN:
                        self.q.put(("back", 0, 0))
                        return 1
                elif self.dragging and wparam == WM_LBUTTONUP:
                    self.dragging = False
                    self.q.put(("up", x, y))
                    return 1
        except Exception:
            pass
        return u.CallNextHookEx(0, ncode, wparam, lparam)

    def run(self):
        h = u.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
        if not h:
            # 被安全软件拦掉 / 权限不足：告诉界面，别让用户对着没反应的鼠标发呆
            self.q.put(("hook_fail", 0, 0))
            return
        # 键盘钩子装不上不影响主流程（Ctrl 还能靠 GetAsyncKeyState 兜），
        # 所以这里失败不报错
        self._kh = u.SetWindowsHookExW(WH_KEYBOARD_LL, self._kproc, None, 0)
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))


class Wizard:
    def __init__(self, tk, cfg):
        self.tk = tk
        self.cfg = cfg
        self.idx = 0
        self.stage = 0
        self.start = None
        self.done = False
        self.q = queue.Queue()
        self._f8 = False
        self._f9 = False
        self.touched = set()          # 本次真正框过 / 按过 F8 的项
        self._force_save = False      # 屏幕变了且有项没重标时，保存要点两次
        self.notice = self._build_notice()

        # ---------------- 底层：全屏透明画框层 ----------------
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.geometry(f"{VIEW_W}x{VIEW_H}+0+0")
        self.root.attributes("-topmost", True)
        self.root.configure(bg=KEY)
        try:
            self.root.attributes("-transparentcolor", KEY)
        except Exception:
            self.root.attributes("-alpha", 0.5)
        self.root.tk.call("tk", "scaling", DPI / 72.0)
        self.cv = tk.Canvas(self.root, width=VIEW_W, height=VIEW_H,
                            bg=KEY, highlightthickness=0)
        self.cv.pack()
        self.root.update_idletasks()

        # 覆盖层要精确铺满虚拟桌面；Tk 的 geometry 表达不了负坐标（多显示器在左/上时），
        # 所以先按 (0,0) 建好，再用 SetWindowPos 移到真实位置。
        hwnd = self.root.winfo_id()
        for h in {hwnd, u.GetParent(ctypes.c_void_p(hwnd))}:
            if h:
                st = u.GetWindowLongW(ctypes.c_void_p(h), -20)          # GWL_EXSTYLE
                u.SetWindowLongW(ctypes.c_void_p(h), -20, st | 0x20)    # WS_EX_TRANSPARENT
        try:
            top = u.GetParent(ctypes.c_void_p(hwnd)) or hwnd
            u.SetWindowPos(ctypes.c_void_p(top), HWND_TOPMOST,
                           VIEW_X, VIEW_Y, VIEW_W, VIEW_H, SWP_NOACTIVATE)
        except Exception:
            pass

        # ---------------- 上层：小面板 ----------------
        # 整个窗口只用一条实线描边，内部不填色块；文字全部画在 Canvas 上并带黑色描边。
        # （Label 直接铺在 -transparentcolor 的透明底上时，抗锯齿边缘会被抠掉一半，
        #   中文小字看起来就是发虚/重影，所以不用 Label。）
        self.panel = tk.Toplevel(self.root)
        self.panel.overrideredirect(True)
        self.panel.attributes("-topmost", True)
        self.panel.configure(bg=KEY)
        try:
            self.panel.attributes("-transparentcolor", KEY)
        except Exception:
            pass
        pw, ph = panel_size(notice=bool(self.notice))
        self.pw, self.ph = pw, ph
        self.px = max(0, SCREEN_W - pw - pxu(24))
        self.py = max(0, SCREEN_H - ph - pxu(24))
        self.panel.geometry(f"{pw}x{ph}+{self.px}+{self.py}")

        self.cvp = tk.Canvas(self.panel, width=pw, height=ph, bg=KEY,
                             highlightthickness=0, bd=0)
        self.cvp.pack(fill="both", expand=True)
        self.cvp.create_rectangle(pxu(1), pxu(1), pw - pxu(1) - 1, ph - pxu(1) - 1,
                                  outline=GREEN, width=max(1, int(pxu(1))), tags="frame")

        # 顶部一条区域按住可拖动面板。必须绑在 canvas 上而不是盖一个 Frame：
        # 透明色 Frame 会覆盖 canvas 已画好的文字，标题就"消失"了。
        self.cvp.bind("<Button-1>", self._drag_start)
        self.cvp.bind("<B1-Motion>", self._drag_move)

        # 按钮行（按钮保留色块，点击目标必须清楚；其余区域全透明）
        btns = tk.Frame(self.panel, bg=KEY)
        self.cvp.create_window(pxu(8), ph - pxu(80), window=btns, anchor="nw",
                               width=pw - pxu(16))
        kbtn = min(UI_K, max(1.0, (pw - pxu(16)) / 560.0))
        mk = dict(font=("Microsoft YaHei", -max(9, int(round(12 * kbtn)))),
                  bg=BTN_BG, fg=WHITE,
                  activebackground="#2a3b48", activeforeground=WHITE, relief="flat",
                  bd=0, padx=max(4, int(round(10 * kbtn))), pady=max(2, int(round(3 * kbtn))))
        tk.Button(btns, text="上一项", command=self.prev_item, **mk).pack(side="left")
        tk.Button(btns, text="跳过 (F8)", command=self.take_default, **mk
                  ).pack(side="left", padx=pxu(6))
        self.btn_save = tk.Button(btns, text="保存并结束", command=self.finish, **mk)
        self.btn_save.pack(side="left")
        tk.Button(btns, text="放弃", command=self.abort, **mk).pack(side="right")

        self.hook_failed = False
        self.hook = _Hook(self.q)
        threading.Thread(target=self.hook.run, daemon=True).start()

        self.redraw()
        self.root.after(60, self.poll)

    # ---------------- 提示 ----------------
    def _build_notice(self):
        src = self.cfg.get("_screen_src")
        if not src:
            return "没有找到标定记录，已按当前屏幕 %dx%d 生成默认位置。" % (SCREEN_W, SCREEN_H)
        if self.cfg.get("_screen_changed"):
            return ("坐标是在 %dx%d 下标的真实屏幕坐标，当前屏幕 %dx%d。\n"
                    "位置对不上请逐项重新框；只框一部分就保存会混写两种屏幕的坐标。"
                    % (src["w"], src["h"], SCREEN_W, SCREEN_H))
        return ""

    # ---------------- 面板拖动 ----------------
    def _drag_start(self, ev):
        # canvas 上除了按钮没有别的可点控件，整块都能拖
        self._drag = True
        self._dx = ev.x_root - self.panel.winfo_x()
        self._dy = ev.y_root - self.panel.winfo_y()

    def _drag_move(self, ev):
        if not getattr(self, "_drag", False):
            return
        nx = max(0, min(SCREEN_W - self.pw, ev.x_root - self._dx))
        ny = max(0, min(SCREEN_H - self.ph, ev.y_root - self._dy))
        self.panel.geometry(f"+{nx}+{ny}")
        self.px, self.py = nx, ny

    # ---------------- 流程 ----------------
    def cur(self):
        return C.SPEC[self.idx]

    def cur_val(self):
        sp = self.cur()
        return self.cfg["regions"].get(sp["key"], C.base_to_screen(sp["default"], sp["kind"]))

    def next_item(self):
        if self.done:
            return
        self.stage = 0
        self.start = None
        self.idx += 1
        if self.idx >= len(C.SPEC):
            self.finish()
            return
        self.redraw()

    def toggle_arm(self):
        """F9 切换：开着的时候不用按 Ctrl，左键直接拖框。"""
        if not getattr(self, "hook", None):
            return
        self.hook.arm = not self.hook.arm
        self.redraw()

    def prev_item(self):
        if self.done:
            return
        self.idx = max(0, self.idx - 1)
        self.stage = 0
        self.start = None
        self.redraw()

    def take_default(self):
        """
        当前项跳过。
        已经有真实标定值的项：原样保留，不覆盖 —— 「跳过」不应该把好好的坐标改成默认值。
        从来没标过的项：才填入按当前屏幕换算的默认位置。
        """
        if self.done:
            return
        sp = self.cur()
        key = sp["key"]
        if key not in self.cfg.get("regions_base", {}):
            self.cfg["regions"][key] = C.base_to_screen(sp["default"], sp["kind"])
        self.next_item()

    def abort(self):
        self.done = False
        try:
            self.root.destroy()
        except Exception:
            pass

    def finish(self):
        if self.done:
            return
        # 屏幕换了 / 从没框过，又有项没在当前屏幕下真实框过：先把风险说清楚，
        # 再点一次才保存。保存下去的永远是「真实屏幕坐标」，不做任何比例换算。
        left = len(C.SPEC) - len(self.touched)
        changed = bool(self.cfg.get("_screen_changed"))
        has_disk = bool(self.cfg.get("_has_disk"))

        # 屏幕变了却一项都没在新屏框过：绝不能保存成「新屏幕标定」
        if changed and not self.touched:
            src = self.cfg.get("_screen_src") or {}
            self._force_save = False
            self._panel_text(pxu(12), self.hint_y, (
                "屏幕是 %dx%d，标定时是 %dx%d，一项都还没在新屏幕下框过。\n"
                "这样保存会把旧坐标当成新屏幕的，主程序会点到错位置。\n"
                "先框一项，或者按「放弃」退出 —— 现有配置不会动。"
                % (SCREEN_W, SCREEN_H, src.get("w", 0), src.get("h", 0))),
                RED, ("Microsoft YaHei", -pxu(12)), width=self.pw - pxu(24), tags="hint")
            self.btn_save.config(text="先框一项")
            return

        partial = left > 0 and (changed or not has_disk)
        if partial and not self._force_save:
            self._force_save = True
            if changed:
                msg = ("还有 %d 项不是在当前屏幕下真实框过的。\n"
                       "保存会把两种屏幕的坐标一起写进 config.json 并标记 partial，\n"
                       "主程序会继续提醒重标。确定要保存就再点一次。" % left)
            else:
                msg = ("还有 %d 项还没有框过，保存会写入程序默认位置并标记 partial。\n"
                       "建议逐项框一遍；确定要保存就再点一次。" % left)
            self._panel_text(pxu(12), self.hint_y, msg, RED,
                             ("Microsoft YaHei", -pxu(12)),
                             width=self.pw - pxu(24), tags="hint")
            self.btn_save.config(text="确认保存")
            return

        self.done = True
        self.cfg["calibrated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # 标定程序是 regions 的最终来源：写当前屏幕的真实坐标 + 当前屏幕信息
        C.save(self.cfg, preserve_regions=False, partial=partial)
        self.cv.delete("all")
        self.cv.create_text(VIEW_W // 2, VIEW_H // 2, text="标定完成，配置已保存",
                            fill=GREEN, font=("Microsoft YaHei", -px(36), "bold"))
        self.panel.destroy()
        self.root.after(1800, self.root.destroy)

    # ---------------- 事件 ----------------
    def poll(self):
        if self.done:
            return
        try:
            while True:
                try:
                    kind, x, y = self.q.get_nowait()
                except queue.Empty:
                    break
                if kind == "down":
                    self.start = (x, y)
                elif kind == "up":
                    if self.start:
                        x1, y1 = self.start
                        self.start = None
                        self.commit(min(x1, x), min(y1, y), max(x1, x), max(y1, y))
                elif kind == "back":
                    self.prev_item()
                elif kind == "hook_fail":
                    self.hook_failed = True
                    self.refresh_panel()
        except Exception:
            pass

        if key_down(VK_CONTROL) and key_down(VK_MENU) and key_down(VK_Q):
            self.abort()
            return

        # F8 只在按下的那一瞬间触发一次，按住不会一路跳到底
        f8 = key_down(VK_F8)
        if f8 and not self._f8:
            self.take_default()
        self._f8 = f8

        # F9 切换标定模式：开着时不用按 Ctrl，左键直接拖框
        f9 = key_down(VK_F9)
        if f9 and not self._f9:
            self.toggle_arm()
        self._f9 = f9

        # 画虚线框/刷新面板任何一步出错都不能让 after 链断掉，
        # 否则后续的 up 事件永远没人处理，表现就是「Ctrl+左键没反应」
        try:
            self.refresh_temp()
        except Exception:
            pass
        try:
            self.root.after(30, self.poll)
        except Exception:
            pass

    def commit(self, x1, y1, x2, y2):
        spec = self.cur()
        if spec["kind"] == "box":
            self.cfg["regions"][spec["key"]] = [x1, y1, x2, y2]
            self.touched.add(spec["key"])
            self.next_item()
        else:
            if self.stage == 0:
                self.cfg["regions"][spec["key"]] = [[x1, y1], [x2, y2]]
                self.stage = 1
                self.redraw()
            else:
                prev = self.cfg["regions"][spec["key"]][0]
                self.cfg["regions"][spec["key"]] = [list(prev),
                                                    [(x1 + x2) // 2, (y1 + y2) // 2]]
                self.touched.add(spec["key"])
                self.next_item()

    # ---------------- 绘制 ----------------
    def refresh_temp(self):
        self.cv.delete("temp")
        if not self.start:
            return
        cx, cy = self._cursor()
        x, y = to_view(*self.start)
        cx, cy = to_view(cx, cy)
        self.cv.create_rectangle(x, y, cx, cy, outline=PINK, width=px(2),
                                 dash=(px(6), px(4)), tags="temp")

    def _cursor(self):
        p = POINT()
        u.GetCursorPos(ctypes.byref(p))
        return int(p.x), int(p.y)

    def redraw(self):
        cv = self.cv
        cv.delete("all")
        if self.done:
            return
        spec = self.cur()

        for i, s in enumerate(C.SPEC):
            if i >= self.idx:
                break
            val = self.cfg["regions"].get(s["key"])
            if s["kind"] == "box" and val:
                cv.create_rectangle(*to_view_box(val), outline=GREEN, width=px(1))

        val = self.cur_val()
        if spec["kind"] == "box" and val:
            vx1, vy1, vx2, vy2 = to_view_box(val)
            cv.create_rectangle(vx1, vy1, vx2, vy2, outline=YELLOW, width=px(2),
                                dash=(px(8), px(6)))
            cx, cy = (vx1 + vx2) // 2, (vy1 + vy2) // 2
            cv.create_line(cx - px(12), cy, cx + px(12), cy, fill=YELLOW, width=px(2))
            cv.create_line(cx, cy - px(12), cx, cy + px(12), fill=YELLOW, width=px(2))
        elif spec["kind"] == "drag" and val:
            sx, sy = to_view(*val[0])
            cv.create_oval(sx - px(10), sy - px(10), sx + px(10), sy + px(10),
                           outline=YELLOW, width=px(3))
            if self.stage == 1:
                ex, ey = to_view(*val[1])
                cv.create_line(sx, sy, ex, ey, fill=CYAN, width=px(3), dash=(px(10), px(8)))
                cv.create_oval(ex - px(14), ey - px(14), ex + px(14), ey + px(14),
                               outline=CYAN, width=px(3))

        self.refresh_panel()

    def _panel_text(self, x, y, text, fill, font, width=None, tags="txt"):
        """透明底上的描边文字：先画一圈黑色描边，再画彩色正文。
        直接把字画在 -transparentcolor 的底色上，抗锯齿边缘会被抠掉一半，看着发虚。"""
        cv = self.cvp
        cv.delete(tags)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            cv.create_text(x + dx, y + dy, text=text, fill="#000000", font=font,
                           anchor="nw", width=width, tags=tags)
        cv.create_text(x, y, text=text, fill=fill, font=font, anchor="nw",
                       width=width, tags=tags)

    def refresh_panel(self):
        spec = self.cur()
        self._force_save = False
        self.btn_save.config(text="保存并结束")
        pw = self.pw
        pad = pxu(14)
        cv = self.cvp
        for t in ("title", "pageno", "notice", "name", "hint", "val", "tip"):
            cv.delete(t)
        cv.delete("prog")

        # 标题行：左侧小标签 + 右侧大号等宽进度
        self._panel_text(pad, pxu(3), "⠿  标定", GREY,
                         ("Microsoft YaHei", -pxu(11)), tags="title")
        self._panel_text(pad + pxu(58), -pxu(1),
                         "%02d / %02d" % (self.idx + 1, len(C.SPEC)), GREEN,
                         ("Consolas", -pxu(20), "bold"), tags="pageno")

        # 16 段细进度条：绿=已完成 青=当前 灰蓝=未做（浅色/深色背景都要看得见）
        n = len(C.SPEC)
        gap = pxu(4)
        segw = (pw - pad * 2 - gap * (n - 1)) / float(n)
        py0, py1 = pxu(32), pxu(37)
        for i in range(n):
            x0 = pad + i * (segw + gap)
            if i < self.idx:
                fill = GREEN
            elif i == self.idx:
                fill = CYAN
            else:
                fill = "#3a4a5a"
            cv.create_rectangle(x0, py0, x0 + segw, py1, fill=fill,
                                outline="", tags="prog")

        y = pxu(44)
        if self.notice:
            self._panel_text(pad, y, self.notice, YELLOW,
                             ("Microsoft YaHei", -pxu(10)), width=pw - pad * 2,
                             tags="notice")
            y += pxu(38)

        self._panel_text(pad, y, spec["label"], WHITE,
                         ("Microsoft YaHei", -pxu(15), "bold"), tags="name")
        y += pxu(24)

        armed = bool(getattr(self, "hook", None) and self.hook.arm)
        hint = ("【标定模式：开】左键直接框选，F9 关闭" if armed
                else "【标定模式：关】Ctrl+左键 框选，F9 打开") + "\n" + spec["hint"]
        if spec["kind"] == "drag":
            hint += "  ·  " + ("先 Ctrl+单击【滑块位置】" if self.stage == 0
                               else "再 Ctrl+单击【拖到的最底位置】")
        self.hint_y = y
        if self.hook_failed:
            # 钩子没装上时，提示必须压过正常提示，否则用户会以为是自己按错了
            self._panel_text(pad, y,
                             "⚠ 鼠标钩子没装上（可能被安全软件拦截），Ctrl+左键 不会生效。\n"
                             "关掉键鼠拦截后重新运行标定程序。",
                             RED, ("Microsoft YaHei", -pxu(11)),
                             width=pw - pad * 2, tags="hint")
        else:
            self._panel_text(pad, y, hint, GREEN if armed else GREY,
                             ("Microsoft YaHei", -pxu(11)),
                             width=pw - pad * 2, tags="hint")

        y += pxu(28)
        self.val_y = y
        self._panel_text(pad, y, "当前值  %s" % (self.cur_val(),), CYAN,
                         ("Consolas", -pxu(12)), tags="val")

        # 底部快捷键：一行细灰字，不抢视觉
        armed = bool(getattr(self, "hook", None) and self.hook.arm)
        hint = ("标定键已开：左键框选    ·    右键 上一项    ·    F8 跳过"
                if armed else
                "Ctrl+左键 框选    ·    Ctrl+右键 上一项    ·    F8 跳过")
        self._panel_text(pad, self.ph - pxu(26), hint,
                         GREEN if armed else GREY,
                         ("Microsoft YaHei", -pxu(10)), tags="tip")


def selftest(seconds=1.5):
    """
    不开交互的自检：建好覆盖层和面板，打印布局信息，然后自动关闭。
    用法：python dvc_calibrate.py --selftest
    """
    import tkinter as tk
    cfg = C.load()
    w = Wizard(tk, cfg)
    w.root.update()

    def bye():
        print("DPI 模式      :", DPI_MODE)
        print("主屏          : %dx%d @%d%%" % (SCREEN_W, SCREEN_H, int(round(S * 100))))
        print("虚拟桌面      : %dx%d at (%d,%d)  显示器 %d 个"
              % (VIEW_W, VIEW_H, VIEW_X, VIEW_Y, SCREEN["monitors"]))
        print("覆盖层        :", w.root.winfo_width(), "x", w.root.winfo_height())
        print("面板尺寸      :", w.pw, "x", w.panel.winfo_height())
        print("换算提示      :", bool(w.notice), "|", (w.notice or "").replace("\n", " "))
        print("当前项        :", w.cur()["label"], "=", w.cur_val())
        print("穿透          :", u.GetWindowLongW(ctypes.c_void_p(w.root.winfo_id()), -20) & 0x20 != 0)
        print("鼠标钩子      :", "失败" if w.hook_failed else "正常")
        w.abort()

    w.root.after(int(seconds * 1000), bye)
    w.root.mainloop()
    return 0


def run(cfg=None):
    import tkinter as tk
    cfg = cfg or C.load()
    w = Wizard(tk, cfg)
    w.root.mainloop()
    return w.done


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    run()
