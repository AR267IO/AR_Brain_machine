# -*- coding: utf-8 -*-
r"""
Win32 底层
==========
这一层只做三件事：

1. **DPI 感知**：进程启动时声明 Per-Monitor V2。不声明的话，1920x1080 在
   125% 缩放下 GetDC 拿到的是 1536x864 的逻辑坐标，点哪儿都偏。
2. **截图**：CreateDIBSection 预分配一块 DIB，BitBlt 直接拷进这块内存。
   按区域缓存，抓 100x30 的小块约 0.3~0.8 ms，够「高速 OCR」用。
3. **输入**：SendInput 发绝对坐标移动 / 点击 / 拖拽 / 按键。

坐标约定
--------
所有坐标都是「主屏左上角为原点」的物理像素，跟 config.json 里的标定值
同一坐标系，中间不做任何换算。

只依赖标准库 + numpy。
"""
import ctypes
import time
from ctypes import wintypes

import numpy as np

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_KEYUP = 0x0002
VK_ESCAPE = 0x1B

# 主屏 GetSystemMetrics 索引
SM_CXSCREEN, SM_CYSCREEN = 0, 1
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


# ---------------------------------------------------------------------------
# 结构体
# ---------------------------------------------------------------------------
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTU(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTU)]


# ---------------------------------------------------------------------------
# 函数签名（x64 下不声明 argtypes 会把指针截成 32 位，典型的坑）
# ---------------------------------------------------------------------------
u32.GetDC.restype = ctypes.c_void_p
u32.GetDC.argtypes = [ctypes.c_void_p]
u32.ReleaseDC.restype = ctypes.c_int
u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
u32.GetSystemMetrics.restype = ctypes.c_int
u32.GetSystemMetrics.argtypes = [ctypes.c_int]
u32.GetDpiForSystem.restype = wintypes.UINT
u32.GetDpiForSystem.argtypes = []
u32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
u32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
u32.GetCursorPos.restype = wintypes.BOOL
u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
u32.SetCursorPos.restype = wintypes.BOOL
u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u32.SendInput.restype = wintypes.UINT
u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
u32.GetAsyncKeyState.restype = ctypes.c_short
u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
u32.GetForegroundWindow.restype = ctypes.c_void_p
u32.GetWindowTextW.restype = ctypes.c_int
u32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]

g32.CreateCompatibleDC.restype = ctypes.c_void_p
g32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
g32.DeleteDC.restype = wintypes.BOOL
g32.DeleteDC.argtypes = [ctypes.c_void_p]
g32.CreateDIBSection.restype = ctypes.c_void_p
g32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(BITMAPINFO),
                                 wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.c_void_p, wintypes.DWORD]
g32.SelectObject.restype = ctypes.c_void_p
g32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
g32.DeleteObject.restype = wintypes.BOOL
g32.DeleteObject.argtypes = [ctypes.c_void_p]
g32.BitBlt.restype = wintypes.BOOL
g32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                       wintypes.DWORD]


# ---------------------------------------------------------------------------
# DPI / 屏幕
# ---------------------------------------------------------------------------
_DPI_MODE = None


def ensure_dpi_aware():
    """声明进程 DPI 感知，必须在建任何窗口、GetDC 之前调用；重复调用无副作用。"""
    global _DPI_MODE
    if _DPI_MODE is not None:
        return _DPI_MODE
    try:
        if u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):   # PER_MONITOR_V2
            _DPI_MODE = "PerMonitorV2"
            return _DPI_MODE
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2):
            _DPI_MODE = "PerMonitor"
            return _DPI_MODE
    except Exception:
        pass
    try:
        if u32.SetProcessDPIAware():
            _DPI_MODE = "System"
            return _DPI_MODE
    except Exception:
        pass
    _DPI_MODE = "none"
    return _DPI_MODE


def screen_info():
    """主屏物理像素 + 虚拟桌面 + DPI"""
    ensure_dpi_aware()
    w, h = u32.GetSystemMetrics(SM_CXSCREEN), u32.GetSystemMetrics(SM_CYSCREEN)
    vx, vy = u32.GetSystemMetrics(SM_XVIRTUALSCREEN), u32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw, vh = u32.GetSystemMetrics(SM_CXVIRTUALSCREEN), u32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    try:
        dpi = int(u32.GetDpiForSystem()) or 96
    except Exception:
        dpi = 96
    return {"w": w, "h": h, "dpi": dpi, "vx": vx, "vy": vy,
            "vw": vw or w, "vh": vh or h}


def cursor_pos():
    p = wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(p))
    return int(p.x), int(p.y)


def foreground_title():
    h = u32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(ctypes.c_void_p(h), buf, 512)
    return buf.value


def key_down(vk):
    return bool(u32.GetAsyncKeyState(vk) & 0x8000)


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------
_HDC_SCREEN = None


def _screen_dc():
    global _HDC_SCREEN
    if _HDC_SCREEN is None:
        _HDC_SCREEN = u32.GetDC(None)
    return _HDC_SCREEN


class Grabber:
    """
    按区域抓屏。

    每个不同的 (w, h) 缓存一套 DC + DIBSection，BitBlt 之后直接从 DIB 内存
    np.frombuffer，省掉 GetDIBits 那次整块拷贝。
    16 个标定区域尺寸各不相同，最多缓存 16 套，内存可以忽略。
    """

    MAX_CACHE = 24

    def __init__(self):
        self._cache = {}
        self._lock = __import__("threading").Lock()

    def _ensure(self, w, h):
        ent = self._cache.get((w, h))
        if ent is not None:
            return ent
        if len(self._cache) >= self.MAX_CACHE:
            for _, old in list(self._cache.items()):
                g32.DeleteObject(old[1])
                g32.DeleteDC(old[0])
            self._cache.clear()
        hdc = g32.CreateCompatibleDC(_screen_dc())
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # 负值 = 自上而下，省一次翻转
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        ppv = ctypes.c_void_p()
        hbmp = g32.CreateDIBSection(_screen_dc(), ctypes.byref(bmi), DIB_RGB_COLORS,
                                    ctypes.byref(ppv), None, 0)
        if not hbmp or not ppv.value:
            raise RuntimeError("CreateDIBSection 失败")
        g32.SelectObject(hdc, hbmp)
        ent = (hdc, hbmp, ppv, w * h * 4)
        self._cache[(w, h)] = ent
        return ent

    def grab(self, x1, y1, x2, y2):
        """抓矩形区域，返回 (h, w, 3) uint8 RGB"""
        w, h = int(x2) - int(x1), int(y2) - int(y1)
        if w <= 0 or h <= 0:
            raise ValueError("抓图区域无效: %r" % ((x1, y1, x2, y2),))
        with self._lock:
            hdc, hbmp, ppv, size = self._ensure(w, h)
            if not g32.BitBlt(hdc, 0, 0, w, h, _screen_dc(), int(x1), int(y1), SRCCOPY):
                raise RuntimeError("BitBlt 失败")
            buf = (ctypes.c_ubyte * size).from_address(ppv.value)
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
            return np.ascontiguousarray(arr[:, :, 2::-1])

    def grab_box(self, box):
        return self.grab(box[0], box[1], box[2], box[3])

    def close(self):
        for _, old in list(self._cache.items()):
            g32.DeleteObject(old[1])
            g32.DeleteDC(old[0])
        self._cache.clear()


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------
def _send_inputs(*items):
    n = len(items)
    arr = (INPUT * n)(*items)
    return u32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi.dx = int(dx)
    inp.mi.dy = int(dy)
    inp.mi.dwFlags = flags
    return _send_inputs(inp)


def precise_sleep(sec):
    """比 time.sleep 准：Windows 的调度粒度约 15.6ms，最后一段用忙等补上。"""
    if sec <= 0:
        return
    end = time.perf_counter() + sec
    if sec > 0.0025:
        time.sleep(sec - 0.002)
    while time.perf_counter() < end:
        pass


class Input:
    """鼠标 / 键盘模拟。所有坐标都是屏幕物理像素。"""

    def __init__(self, screen=None):
        self.screen = screen or screen_info()
        self.sw = max(1, self.screen["w"])
        self.sh = max(1, self.screen["h"])

    # --- 移动 ---
    def _to_abs(self, x, y):
        nx = int(round(x * 65535.0 / (self.sw - 1)))
        ny = int(round(y * 65535.0 / (self.sh - 1)))
        return max(0, min(65535, nx)), max(0, min(65535, ny))

    def move_to(self, x, y):
        nx, ny = self._to_abs(x, y)
        _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, nx, ny)

    def move_to_hard(self, x, y):
        """直接 SetCursorPos：比 SendInput 更硬，游戏兼容性差一点但更准"""
        u32.SetCursorPos(int(x), int(y))

    def move_smooth(self, x1, y1, x2, y2, steps=16, duration=0.12):
        """拖拽用的平滑移动：中途必须有中间点，光有首尾两次移动游戏不认"""
        steps = max(2, int(steps))
        step_sleep = max(0.0, duration) / steps
        for i in range(1, steps + 1):
            t = i / float(steps)
            x = x1 + (x2 - x1) * t
            y = y1 + (y2 - y1) * t
            self.move_to(x, y)
            precise_sleep(step_sleep)

    # --- 点击 ---
    def click_at(self, x, y, hold=0.035, move_first=True, move_wait=0.006,
                 double=False, gap=0.09):
        if move_first:
            self.move_to(x, y)
            # 挪过去要留够时间：游戏内部光标/准星是平滑跟随的，
            # 刚发完移动就按下，落点可能还在半路上，点击就空了。
            precise_sleep(move_wait)
        # double=True 就是隔 gap 再点一次，光标不动
        for i in range(2 if double else 1):
            if i:
                precise_sleep(gap)
            _mouse(MOUSEEVENTF_LEFTDOWN)
            precise_sleep(hold)
            _mouse(MOUSEEVENTF_LEFTUP)

    def click_box(self, box, hold=0.035):
        cx = (box[0] + box[2]) // 2
        cy = (box[1] + box[3]) // 2
        self.click_at(cx, cy, hold=hold)

    # --- 拖拽 ---
    def drag(self, p1, p2, hold=0.03, steps=18, duration=0.14, settle=0.06):
        """从 p1 拖到 p2。起手先按下去停一下，游戏才认这次拖动。"""
        x1, y1 = p1
        x2, y2 = p2
        self.move_to(x1, y1)
        precise_sleep(0.02)
        _mouse(MOUSEEVENTF_LEFTDOWN)
        precise_sleep(hold)
        self.move_smooth(x1, y1, x2, y2, steps=steps, duration=duration)
        precise_sleep(settle)
        _mouse(MOUSEEVENTF_LEFTUP)

    # --- 键盘 ---
    def key_press(self, vk, hold=0.03):
        down = INPUT(type=INPUT_KEYBOARD)
        down.ki.wVk = vk
        up = INPUT(type=INPUT_KEYBOARD)
        up.ki.wVk = vk
        up.ki.dwFlags = KEYEVENTF_KEYUP
        _send_inputs(down)
        precise_sleep(hold)
        _send_inputs(up)

    def press_escape(self, times=1, gap=0.13):
        for i in range(int(times)):
            self.key_press(VK_ESCAPE)
            if i != times - 1:
                precise_sleep(gap)
