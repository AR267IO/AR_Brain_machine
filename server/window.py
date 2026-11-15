# -*- coding: utf-8 -*-
"""
窗口置顶与尺寸调整。

网页自己没法把浏览器窗口设成置顶，只能由后端调 Win32。做法是前端点「开始运行」
时先调 /api/pin，后端取当前前台窗口（也就是用户刚点按钮的那个浏览器窗口），
记下它原来的位置大小，再改成置顶 + 小窗口；结束时原样还原。
"""
import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL


def front_window():
    """当前前台窗口，拿不到就返回 None。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd or not user32.IsWindowVisible(hwnd):
        return None
    return hwnd


def screen_size():
    try:
        return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    except Exception:
        return (1920, 1080)


def rect_of(hwnd):
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def dpi_scale(hwnd=None):
    """当前窗口（或系统）的缩放比例，200% 缩放时是 2.0。"""
    try:
        if hwnd:
            d = ctypes.windll.user32.GetDpiForWindow(wintypes.HWND(hwnd))
            if d:
                return d / 96.0
    except Exception:
        pass
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def webview_place(w, h, x=None, y=None):
    """让内嵌的 WebView2 跟着换版面和位置。

    只用 SetWindowPos 改原生窗口大小，WebView2 自己的绘制区域不会跟着变，
    结果就是窗口缩小后只有左上角一块有内容、其余是白的，得用鼠标点一下才刷新。
    走 pywebview 的 resize/move 才能把新尺寸通知到里面那层。
    pywebview 的这两个接口吃的是逻辑像素，所以要先按 DPI 折回去。"""
    try:
        import webview
        wins = getattr(webview, "windows", None)
        if not wins:
            return False
        win = wins[0]
        s = dpi_scale() or 1.0
        win.resize(max(120, int(round(w / s))), max(80, int(round(h / s))))
        if x is not None and y is not None:
            win.move(int(round(x / s)), int(round(y / s)))
        return True
    except Exception as e:
        # resize 得在 UI 线程上跑，从 HTTP 线程调可能被 WinForms 拒掉；
        # 拒掉了就走 SetWindowPos 兜底，这条日志留着排查白边用。
        try:
            import os
            import time as _t
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "win_debug.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write("%s webview_place(%s,%s,%s,%s) failed: %r\n"
                        % (_t.strftime("%H:%M:%S"), w, h, x, y, e))
        except Exception:
            pass
        return False


RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100


def repaint(hwnd):
    """改完尺寸后强制整窗重绘一次。

    WebView2 是挂在 Form 里的独立子窗口，Form 尺寸变了它不一定马上重排，
    于是小窗右侧会留一块白的、点一下才恢复。RedrawWindow 带上 ALLCHILDREN
    能把里面那层也一起刷掉。"""
    try:
        user32.RedrawWindow(hwnd, None, None,
                            RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW)
        return True
    except Exception:
        return False


def _schedule_repaint(hwnd, w=None, h=None, x=None, y=None):
    """pywebview 的 resize 排在它自己的消息循环里，立刻刷可能刷不到，
    所以隔两个时间点再补刷，把「只画了左上角」的残影顶掉。
    中间那一次顺手把尺寸再通知一遍：同尺寸重复通知能逼 WebView2 重新布局，
    比单纯重绘更管用。"""
    repaint(hwnd)
    if w and h:
        threading.Timer(0.18, lambda: (webview_place(w, h, x, y), repaint(hwnd))).start()
    else:
        threading.Timer(0.18, lambda: repaint(hwnd)).start()
    threading.Timer(0.55, lambda: repaint(hwnd)).start()


def apply(hwnd, x, y, w, h, topmost):
    """设位置/尺寸/层级。topmost 传 True 置顶，False 取消。

    无边框全屏的游戏会把整屏幕的合成权攥住，这时 WebView2 认为自己的窗口
    被挡住了，会停止重排和绘制——缩小后右边留一块白、放大后内容还停在上一个
    尺寸，点一下才恢复。所以改尺寸时先把窗口藏起来：窗口不可见的这段时间里
    WebView2 不会做遮挡判断，会老老实实按新尺寸重排，显示出来就是对的。
    """
    order = HWND_TOPMOST if topmost else HWND_NOTOPMOST
    hidden = False
    try:
        user32.ShowWindow(hwnd, SW_HIDE)
        hidden = True
        time.sleep(0.06)
    except Exception:
        pass

    moved = webview_place(w, h, x, y)
    if moved:
        # 尺寸位置都交给 pywebview 了，这里只改层级。
        # 不能在它之后再 SetWindowPos 改位置：pywebview 的 resize 是异步的，
        # 后到的回调会把我们刚设的坐标又挪走（实测会偏 384x54）。
        user32.SetWindowPos(hwnd, order, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        # resize 是异步的，藏着的这段时间要等它真的把尺寸改到位再显示，
        # 否则放大时 WebView2 还没接到新尺寸就重新可见，又会卡在旧尺寸上。
        deadline = time.time() + 1.2
        while time.time() < deadline:
            r = rect_of(hwnd)
            if r and r[2] == int(w) and r[3] == int(h):
                break
            time.sleep(0.03)
        time.sleep(0.08)
        if hidden:
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        _schedule_repaint(hwnd, w, h, x, y)
        return True
    ok = bool(user32.SetWindowPos(hwnd, order, int(x), int(y), int(w), int(h),
                                  SWP_SHOWWINDOW))
    _schedule_repaint(hwnd, w, h, x, y)
    return ok


def set_size(hwnd, w, h):
    """保持左上角不动，只改宽高。"""
    r = rect_of(hwnd)
    if not r:
        return False
    return apply(hwnd, r[0], r[1], w, h, True)


def work_size():
    """工作态的小窗口尺寸（物理像素）。按屏幕比例算，跨 DPI 才一致。
    待机窗口是 0.54 x 0.62，取它的边长一半就是面积 1/4：
    3200x2000 上 = 864x620，窗口里还有 434x310 逻辑像素，够放余额和几行日志。"""
    sw, sh = screen_size()
    return max(620, int(sw * 0.27)), max(440, int(sh * 0.31))


def idle_size():
    sw, sh = screen_size()
    return max(860, int(sw * 0.54)), max(600, int(sh * 0.62))


def raise_once(hwnd):
    """把窗口提到最前并切过去，但不留常驻置顶。
    顶一下再放开 TOPMOST 是绕开 Windows 前台锁的常用手法。"""
    if not hwnd:
        return False
    try:
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


class Pin:
    """记住窗口原始的位置大小，退出工作态时还原。"""

    def __init__(self):
        self.hwnd = None
        self.home = None

    @property
    def active(self):
        return self.hwnd is not None

    def enter(self, w=None, h=None, hwnd=None):
        """把窗口置顶，给了 w/h 就顺便缩成那么小。
        hwnd 给定时就用它，否则取当前前台窗口。已经置顶过就不重复记录原始位置。"""
        if self.active:
            if w and h:
                set_size(self.hwnd, w, h)
            return True
        hwnd = hwnd or front_window()
        if not hwnd:
            return False
        home = rect_of(hwnd)
        if not home:
            return False
        self.hwnd = hwnd
        self.home = home
        x, y, ow, oh = home
        if w and h:
            # 太靠右下会把窗口顶出屏幕，先夹一下
            sw, sh = screen_size()
            if x + w > sw:
                x = max(0, sw - w)
            if y + h > sh:
                y = max(0, sh - h)
            apply(hwnd, x, y, w, h, True)
        else:
            apply(hwnd, x, y, ow, oh, True)
        return True

    def leave(self):
        """取消置顶并还原原来的位置大小。"""
        if not self.active:
            return False
        hwnd, home = self.hwnd, self.home
        self.hwnd, self.home = None, None
        apply(hwnd, home[0], home[1], home[2], home[3], False)
        # 关键顺序：先让窗口拿到焦点，WebView2 才肯按新尺寸重排。
        # 窗口不在前台时（比如游戏在前台），光改尺寸它是不会跟手的，
        # 结果就是窗口变大了、内容还停在小窗那块，右边一大片白。
        raise_once(hwnd)
        time.sleep(0.35)
        apply(hwnd, home[0], home[1], home[2], home[3], False)
        return True

    def keep_top(self):
        """工作态期间定期调用，防止别的窗口抢走置顶。
        只改层级不改位置尺寸，所以不会闪。"""
        if not self.active:
            return False
        ok = user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        return bool(ok)
