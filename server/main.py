# -*- coding: utf-8 -*-
r"""
程序入口。

起一个本地 HTTP 服务（后台线程）托管前端，再用 pywebview 开一个原生窗口把它装进去。
用户看到的是一个普通桌面程序，不是浏览器——没有地址栏、没有标签页。

窗口的置顶与缩放不在这里做，由 server\window.py 通过 Win32 处理，
这样「工作态钉在最前」和「结束后还原」都只依赖窗口句柄，跟用什么外壳无关。
"""
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # 打包后代码在 _internal 里，数据目录在 exe 旁边
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(HERE)
    sys.path.insert(0, HERE)

from app_handler import Handler, ENGINE, pick_port  # noqa: E402

TITLE = "格拉默脑机"


def _center_rect(w, h):
    try:
        import ctypes
        user32 = ctypes.windll.user32
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        return max(0, (sw - w) // 2), max(0, (sh - h) // 2)
    except Exception:
        return 60, 60


def _screen():
    try:
        import ctypes
        u = ctypes.windll.user32
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080


def _find_hwnd(title, timeout=6.0):
    """按标题找窗口。pywebview 不给原生句柄，只能这么捞。"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    end = time.time() + timeout
    while time.time() < end:
        hit = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def cb(h, _):
            n = u.GetWindowTextLengthW(h)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, buf, n + 1)
                if buf.value == title:
                    hit.append(h)
                    return False
            return True

        u.EnumWindows(cb, 0)
        if hit:
            return hit[0]
        time.sleep(0.15)
    return None


def _place(w, h):
    """按物理像素把窗口摆到屏幕正中。create_window 的尺寸在 200% 缩放下会被
    再乘一遍 DPI，量出来是错的，所以开窗后用 SetWindowPos 重放一次。"""
    import ctypes
    hwnd = _find_hwnd(TITLE)
    if not hwnd:
        return False
    x, y = _center_rect(w, h)
    ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0040)
    return True


def _polish(hwnd):
    """深色标题栏 + 自己的图标。系统标题栏默认是白的，压在深色界面上很跳。"""
    import ctypes
    from ctypes import byref, c_int
    try:
        dwm = ctypes.windll.dwmapi
        on = c_int(1)
        for attr in (20, 19):        # 20 是 Win10 20H1+，19 是更早的版本
            dwm.DwmSetWindowAttribute(hwnd, attr, byref(on), ctypes.sizeof(on))
    except Exception:
        pass

    try:
        u = ctypes.windll.user32
        ico = os.path.join(BASE, "web", "app.ico")
        if os.path.isfile(ico):
            h = u.LoadImageW(None, ico, 1, 0, 0, 0x0010)   # IMAGE_ICON, LR_LOADFROMFILE
            if h:
                u.SendMessageW(hwnd, 0x0080, 1, h)          # WM_SETICON, ICON_BIG
                u.SendMessageW(hwnd, 0x0080, 0, h)          # WM_SETICON, ICON_SMALL
    except Exception:
        pass


def _patch_webview2_args():
    """让 WebView2 别因为「被别的窗口盖住」就停止绘制。

    无边框全屏的游戏会攥住整个屏幕的合成权，WebView2 判定自己被遮挡后就把
    绘制停掉——小窗右边留白、内容停在上一个尺寸，点一下才恢复。这条判定由
    CalculateNativeWinOcclusion 控制，关掉它就好。

    问题是 pywebview 把 WebView2 的 AdditionalBrowserArguments 写死在
    platforms/edgechromium.py 里，会盖掉同名环境变量，所以只能去改它的源码。
    每次启动都检查一次，重装了 pywebview 也能自己修回来。
    """
    try:
        import importlib.util
        spec = importlib.util.find_spec("webview.platforms.edgechromium")
        path = spec.origin if spec else None
        if not path or not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if "--disable-gpu" in src:
            return True
        # 已经打过 CalculateNativeWinOcclusion 但还没 disable-gpu 的旧版本，补上。
        # GPU 合成才是白屏的根：软渲染之后放大/缩小都正常。
        if "CalculateNativeWinOcclusion" in src:
            fixed = src.replace(
                "'--disable-features=ElasticOverscroll,CalculateNativeWinOcclusion'",
                "'--disable-gpu --disable-features=ElasticOverscroll,CalculateNativeWinOcclusion'")
            if fixed != src:
                open(path, "w", encoding="utf-8").write(fixed)
                return True
        if "CalculateNativeWinOcclusion" in src:
            src = src.replace(
                "'--disable-features=ElasticOverscroll,CalculateNativeWinOcclusion'",
                "'--disable-gpu --disable-features=ElasticOverscroll,CalculateNativeWinOcclusion'")
            open(path, 'w', encoding='utf-8').write(src)
            return True
        old = "props.AdditionalBrowserArguments = '--disable-features=ElasticOverscroll'"
        if old not in src:
            return False
        new = (
            "props.AdditionalBrowserArguments = (\n"
            "            '--disable-features=ElasticOverscroll,CalculateNativeWinOcclusion'\n"
            "            ' --disable-backgrounding-occluded-windows'\n"
            "            ' --disable-renderer-backgrounding'\n"
            "            ' --disable-background-timer-throttling'\n"
            "        )"
        )
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(src.replace(old, new, 1))
        return True
    except Exception:
        return False


def main():
    headless = "--headless" in sys.argv
    port = pick_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % port

    cfg = ENGINE.cfg
    print("=" * 56)
    print(" 三角洲自动化")
    print(" 界面端口 %d ｜ 配置 %s（%d 个区域）"
          % (port, os.path.basename(cfg.get("path") or "—"),
             len(cfg.get("regions") or {})))
    print("=" * 56, flush=True)

    if headless:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            srv.shutdown()
        return

    _patch_webview2_args()

    try:
        import webview
    except ImportError:
        print("\n缺少 pywebview，装一下：")
        print("  python -m pip install pywebview -i https://pypi.tuna.tsinghua.edu.cn/simple")
        input("按回车关闭。")
        return

    import ctypes
    ctypes.windll.user32.SetProcessDPIAware()
    sw, sh = _screen()
    # 待机窗口按屏幕比例定，不写死像素——200% 缩放下才不会变成一小坨
    win_w, win_h = ENGINE.win_size()

    webview.create_window(
        TITLE, url,
        width=win_w, height=win_h,
        # 下限要放得比工作态小窗还小，否则 pywebview 会把 SetWindowPos
        # 缩小的窗口又弹回去（它的下限是逻辑像素，200% 缩放下要再乘 2）
        min_size=(200, 150),
        resizable=True,
        text_select=False,
    )

    def _after_start():
        # pywebview 在窗口真正 show 出来之前就返回了，这时候设位置会被它自己覆盖，
        # 所以等一下再放，放完再补一次。
        time.sleep(0.6)
        hwnd = _find_hwnd(TITLE, 3.0)
        if not hwnd:
            return
        ENGINE.win_hwnd = hwnd      # 置顶/缩窗都靠它，别再让 Pin 去猜前台窗口
        import ctypes
        x, y = _center_rect(win_w, win_h)
        sp = ctypes.windll.user32.SetWindowPos
        for delay in (0.0, 0.8, 2.0):
            if delay:
                time.sleep(delay)
            sp(hwnd, 0, x, y, win_w, win_h, 0x0040)
        # 每次启动都主动抢一下前台，不然双击启动后窗口可能压在别的程序后面
        try:
            u = ctypes.windll.user32
            flags = 0x0002 | 0x0001 | 0x0010      # NOMOVE|NOSIZE|NOACTIVATE
            sp(hwnd, -1, 0, 0, 0, 0, flags)       # HWND_TOPMOST 顶一下
            sp(hwnd, -2, 0, 0, 0, 0, flags)       # 立刻取消，只借它把窗口提到最前
            u.SetForegroundWindow(hwnd)
        except Exception:
            pass
        _polish(hwnd)

    try:
        webview.start(_after_start)
    finally:
        srv.shutdown()


def _run_embedded(entry, argv0):
    """打包后的自调用：同一个 exe 换个身份跑子程序。"""
    # 输出走管道时 Windows 会按 GBK 写，前端按 UTF-8 解就成乱码了
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    rest = [a for a in sys.argv[1:] if a != entry]
    sys.argv = [argv0] + rest
    if argv0 == "dvc_auto.py":
        import dvc_auto
        return dvc_auto.main()
    # 标定程序：dvc_config 默认往自己所在目录写 config.json，打包后那是 _internal，
    # 所以这里把它接到安装目录的 auto\\config.json 上
    cfg_path = os.path.join(BASE, "auto", "config.json")
    import dvc_config as C
    _orig_save = C.save
    def _save(cfg, path=None, **kw):
        return _orig_save(cfg, path or cfg_path, **kw)
    C.save = _save
    import dvc_calibrate
    dvc_calibrate.run(C.load(cfg_path))
    return 0


if __name__ == "__main__":
    if "--run-auto" in sys.argv:
        sys.exit(_run_embedded("--run-auto", "dvc_auto.py"))
    if "--run-calib" in sys.argv:
        sys.exit(_run_embedded("--run-calib", "dvc_calibrate.py"))
    main()
