# -*- coding: utf-8 -*-
r"""
坐标配置层
-----------
* 所有需要标定的位置都定义在 SPEC 里（代号、说明、类型、基准默认值）
* SPEC 里的 default 一律按基准分辨率 BASE_W x BASE_H（3200 x 2000）记录，
  程序启动时按当前屏幕等比换算，所以换分辨率/换缩放不会一上来就全错
* 标定结果写入 config.json，并记录标定当时的屏幕（分辨率 + DPI）
* 读取时如果屏幕和标定时不一致，把坐标等比换算到当前屏幕：
  只改内存，不写回磁盘 —— 避免每换一次分辨率就二次缩放，越缩越偏

config.json 结构（version 2）：
{
  "version": 2,
  "calibrated_at": "2026-11-14 04:56:06",
  "screen": {"w": 3200, "h": 2000, "dpi": 192},
  "regions": { ... },
  "options": { ... }
}

version 1 的旧文件没有 "screen" 字段，读取时按 BASE_W x BASE_H 处理。
"""
import ctypes
import json
import os
import sys

# PyInstaller 打包后 __file__ 指向临时解压目录，配置必须放在 exe 旁边，
# 否则标定结果写进临时目录，程序一关就没了
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
LOG_PATH = os.path.join(BASE, "run.log")

# SPEC 默认值记录的基准分辨率（当初真实标定时的物理分辨率）
BASE_W, BASE_H = 3200, 2000
GAME_W, GAME_H = BASE_W, BASE_H          # 兼容旧名字
CONFIG_VERSION = 2

# kind: box = 一个矩形区域（点击取中心 / OCR 取整块）
#       drag = 两个点（起点、终点）
SPEC = [
    dict(key="btn_peizhuang", label="主菜单「配装」按钮", kind="box",
         hint="主菜单右下绿条左半边", default=[2440, 1680, 2745, 1745]),
    dict(key="tab_cangku", label="顶部「仓库」页签", kind="box",
         hint="顶部导航第 2 项", default=[470, 168, 580, 210]),
    dict(key="ocr_balance", label="右上角余额数字", kind="box",
         hint="只框数字本身，别带到旁边货币图标", default=[2465, 168, 2610, 208]),
    dict(key="scan_blank", label="仓库·中间装备区（找「空白」）", kind="box",
         hint="只框中间区，右侧仓库格子必须排除", default=[1120, 280, 1980, 1700]),
    dict(key="btn_cart", label="仓库·购物车图标", kind="box",
         hint="中间面板右侧竖排工具栏底部", default=[1960, 1652, 2059, 1732]),
    dict(key="btn_sell", label="仓库·右下「出售」", kind="box",
         hint="点了购物车之后出现的绿框按钮", default=[2400, 1660, 2760, 1760]),
    dict(key="btn_sell_ok", label="出售弹窗里的「出售」", kind="box",
         hint="军需处回收价 500,000 那一行右边", default=[2186, 837, 2500, 912]),
    dict(key="slot_helmet", label="配装页·头盔槽", kind="box",
         hint="左侧「头盔」那一格", default=[150, 700, 660, 880]),
    dict(key="slot_armor", label="配装页·护甲槽", kind="box",
         hint="左侧「防弹衣」那一格", default=[150, 900, 660, 1060]),
    dict(key="drag_scroll", label="选择页·滚动条（拖到最底）", kind="drag",
         hint="先标滑块位置，再标屏幕最底端", default=[[722, 500], [722, 1990]]),
    dict(key="click_a", label="选择页·刷价点 A", kind="box",
         hint="列表倒数第二行", default=[183, 1349, 671, 1481]),
    dict(key="click_b", label="选择页·刷价点 B", kind="box",
         hint="列表最后一行", default=[186, 1567, 682, 1677]),
    dict(key="ocr_price", label="选择页·最低行价格数字", kind="box",
         hint="最后一行右侧那串数字", default=[540, 1663, 698, 1715]),
    dict(key="btn_buy_1", label="选择页·购买按钮 1", kind="box",
         hint="右下第一个按钮", default=[2600, 1502, 3023, 1554]),
    dict(key="btn_buy_2", label="选择页·购买按钮 2", kind="box",
         hint="右下第二个按钮", default=[2611, 1625, 3019, 1675]),
    dict(key="click_center", label="兜底·点屏幕中心", kind="box",
         hint="ESC 之后点这里回初始页", default=[1560, 960, 1640, 1040]),
    # kind="shot"：框完不是记坐标，而是把这块屏幕截图存成物品模板
    dict(key="item_blank", label="仓库里的「空白存储」图标", kind="shot",
         hint="框住一个完整物品格（连格子边框一起），截图会存成识别模板",
         default=[1115, 300, 1420, 700]),
]

KEYS = [s["key"] for s in SPEC]
SPEC_BY_KEY = {s["key"]: s for s in SPEC}

DEFAULT_OPTIONS = {
    "price_min": 200000,        # 抢购：价格下限（含）
    "price_max": 300000,        # 抢购：价格上限（含）
    "balance_low": 300000,      # 余额低于此值 -> 补充余额
    "balance_high": 800000,     # 余额高于此值 -> 报错暂停
    "refresh_cycle_sec": 600,   # 每 10 分钟回初始页重置一次
    "target": "helmet",         # helmet / armor
    "no_decimal": True,         # OCR 结果里忽略小数点
}


# ---------------------------------------------------------------------------
# DPI 与屏幕
# ---------------------------------------------------------------------------
_DPI_MODE = None


def ensure_dpi_aware():
    """
    声明进程 DPI 感知，必须在创建任何窗口之前调用；重复调用无副作用。

    优先 Per-Monitor V2：这样 200% / 250% 缩放下拿到的分辨率、鼠标坐标、
    窗口尺寸都是物理像素，不会被系统偷偷缩放一半。
    """
    global _DPI_MODE
    if _DPI_MODE is not None:
        return _DPI_MODE
    u = ctypes.windll.user32
    try:
        u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):   # PER_MONITOR_V2
            _DPI_MODE = "PerMonitorV2"
            return _DPI_MODE
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2):        # PER_MONITOR
            _DPI_MODE = "PerMonitor"
            return _DPI_MODE
    except Exception:
        pass
    try:
        if u.SetProcessDPIAware():                                # SYSTEM
            _DPI_MODE = "System"
            return _DPI_MODE
    except Exception:
        pass
    _DPI_MODE = "none"
    return _DPI_MODE


def _metric(index, fallback=0):
    try:
        return int(ctypes.windll.user32.GetSystemMetrics(index))
    except Exception:
        return fallback


def screen_info():
    """
    当前屏幕信息。

    w/h   : 主显示器物理像素（游戏全屏时的渲染分辨率，也是坐标换算的基准）
    vx/vy/vw/vh : 虚拟桌面（多显示器合计），覆盖层要铺满这一块
    dpi   : 主显示器 DPI（96 = 100%，192 = 200%）
    """
    ensure_dpi_aware()
    u = ctypes.windll.user32
    w, h = _metric(0), _metric(1)
    vx, vy = _metric(76), _metric(77)
    vw, vh = _metric(78), _metric(79)
    dpi = 96
    try:
        dpi = int(u.GetDpiForSystem())
    except Exception:
        try:
            hdc = u.GetDC(0)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))   # LOGPIXELSX
            u.ReleaseDC(0, hdc)
        except Exception:
            dpi = 96
    if dpi <= 0:
        dpi = 96
    return {
        "w": w or BASE_W, "h": h or BASE_H, "dpi": dpi,
        "vx": vx, "vy": vy,
        "vw": vw or w or BASE_W, "vh": vh or h or BASE_H,
        "monitors": _metric(80, 1) or 1,
    }


def screen_tag(info=None):
    s = info or screen_info()
    return "%dx%d@%d" % (s["w"], s["h"], s["dpi"])


def screen_brief(info):
    """写进 config.json 的屏幕字段（只留换算要用的三项）"""
    return {"w": int(info["w"]), "h": int(info["h"]), "dpi": int(info["dpi"])}


# ---------------------------------------------------------------------------
# 坐标换算
# ---------------------------------------------------------------------------
def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def scale_point(x, y, sx, sy, w, h):
    """把一个点从基准坐标换算到目标屏幕，并夹在屏幕内"""
    nx = int(round(x * sx))
    ny = int(round(y * sy))
    return (_clamp(nx, 0, max(0, w - 1)), _clamp(ny, 0, max(0, h - 1)))


def scale_region(val, kind, sx, sy, w, h):
    """
    换算一项坐标。
    box : [x1,y1,x2,y2] -> 保证 x1<=x2、y1<=y2，且宽高至少 1px（否则点不中）
    drag: [[x1,y1],[x2,y2]] -> 两个点分别换算
    """
    if kind == "drag":
        (x1, y1), (x2, y2) = val
        return [list(scale_point(x1, y1, sx, sy, w, h)),
                list(scale_point(x2, y2, sx, sy, w, h))]
    x1, y1, x2, y2 = val
    nx1, ny1 = scale_point(min(x1, x2), min(y1, y2), sx, sy, w, h)
    nx2, ny2 = scale_point(max(x1, x2), max(y1, y2), sx, sy, w, h)
    nx1, nx2 = _fix_span(nx1, nx2, w)
    ny1, ny2 = _fix_span(ny1, ny2, h)
    return [nx1, ny1, nx2, ny2]


def _fix_span(a, b, limit):
    """
    保证 0 <= a < b <= limit-1，至少留 1px。
    缩小到极小框、或源坐标本身贴着屏幕边缘时，向内收一格，不让框退化成一条线。
    """
    limit = max(1, int(limit))
    a = _clamp(a, 0, limit - 1)
    b = _clamp(b, 0, limit - 1)
    if b > a:
        return a, b
    if a + 1 <= limit - 1:
        return a, a + 1
    return max(0, limit - 2), limit - 1


def rescale_regions(regions, src_w, src_h, dst_w, dst_h):
    """
    把一整套坐标从 src 分辨率换算到 dst 分辨率（等比拉伸，不修改入参）。
    横纵比不同时按各自比例拉伸 —— 游戏 HUD 一般按百分比布局，这是最接近的一阶近似。
    """
    src_w = src_w or BASE_W
    src_h = src_h or BASE_H
    dst_w = dst_w or BASE_W
    dst_h = dst_h or BASE_H
    sx = dst_w / float(src_w)
    sy = dst_h / float(src_h)
    out = {}
    for key, val in regions.items():
        if not val:
            continue
        spec = SPEC_BY_KEY.get(key)
        kind = spec["kind"] if spec else ("drag" if _looks_like_drag(val) else "box")
        out[key] = scale_region(val, kind, sx, sy, dst_w, dst_h)
    return out


def _looks_like_drag(val):
    try:
        return (len(val) == 2 and len(val[0]) == 2 and len(val[1]) == 2)
    except Exception:
        return False


def base_to_screen(val, kind, screen=None):
    """SPEC 默认值（基准分辨率）-> 当前屏幕"""
    s = screen or screen_info()
    sx = s["w"] / float(BASE_W)
    sy = s["h"] / float(BASE_H)
    return scale_region(val, kind, sx, sy, s["w"], s["h"])


# ---------------------------------------------------------------------------
# 读 / 写
# ---------------------------------------------------------------------------
def default_config(screen=None):
    """默认配置：regions 是 SPEC 默认值换算到当前屏幕后的结果"""
    s = screen or screen_info()
    return {
        "version": CONFIG_VERSION,
        "calibrated_at": "",
        "screen": screen_brief(s),
        "regions": {sp["key"]: base_to_screen(sp["default"], sp["kind"], s) for sp in SPEC},
        "options": dict(DEFAULT_OPTIONS),
    }


def load(path=CONFIG_PATH, rescale=False):
    """
    读配置。

    默认 rescale=False：**只给真实坐标**，也就是标定时在屏幕上实际框出来的值。
    磁盘上存的是什么，读出来就是什么，不做任何换算 —— config.json 永远是
    「标定那一刻、相对屏幕左上角的物理像素坐标」。

    * 文件不存在 / 损坏 -> 返回默认配置（十六项都是换算后的默认值，不算已标定）
    * 文件是 version 1（没有 screen 字段）-> 按 BASE_W x BASE_H 处理
    * rescale=True 是给「临时试跑」用的可选能力：屏幕和标定时不同时，
      在内存里按比例换算一份，不写盘。默认不开，免得把换算值当成标定值。
    * 以 _ 开头的键是内部状态，save 时不会写盘
    """
    disk = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                disk = json.load(f) or {}
            if not isinstance(disk, dict):
                disk = {}
        except Exception:
            disk = {}

    cur = screen_info()
    cfg = default_config(cur)

    disk_regions = disk.get("regions") or {}
    if not isinstance(disk_regions, dict):
        disk_regions = {}
    disk_opts = disk.get("options") or {}
    if not isinstance(disk_opts, dict):
        disk_opts = {}
    src = disk.get("screen") or {}
    if not isinstance(src, dict):
        src = {}

    src_w = int(src.get("w") or 0) or BASE_W
    src_h = int(src.get("h") or 0) or BASE_H
    src_dpi = int(src.get("dpi") or 0) or 0

    # 原始（标定时）坐标：换算失败时用来回退
    cfg["regions_base"] = {}
    for key, val in disk_regions.items():
        if not val:
            continue
        spec = SPEC_BY_KEY.get(key)
        kind = spec["kind"] if spec else ("drag" if _looks_like_drag(val) else "box")
        try:
            if kind == "drag":
                if len(val) != 2 or len(val[0]) != 2 or len(val[1]) != 2:
                    continue
                norm = [[int(val[0][0]), int(val[0][1])],
                        [int(val[1][0]), int(val[1][1])]]
            else:
                if len(val) != 4:
                    continue
                norm = [int(v) for v in val]
        except Exception:
            continue
        cfg["regions_base"][key] = norm

    if rescale and (src_w, src_h) != (cur["w"], cur["h"]) and cfg["regions_base"]:
        converted = rescale_regions(cfg["regions_base"], src_w, src_h, cur["w"], cur["h"])
        cfg["regions"].update(converted)
        did_rescale = True
    else:
        cfg["regions"].update(cfg["regions_base"])
        did_rescale = False

    cfg["options"].update(disk_opts)
    cfg["calibrated_at"] = disk.get("calibrated_at", "") or ""
    cfg["version"] = int(disk.get("version") or 1)
    cfg["partial"] = bool(disk.get("partial"))

    cfg["_screen_src"] = ({"w": src_w, "h": src_h, "dpi": src_dpi} if disk_regions else None)
    cfg["_screen_cur"] = dict(cur)
    cfg["_screen_changed"] = bool(cfg["regions_base"]) and (src_w, src_h) != (cur["w"], cur["h"])
    cfg["_rescaled"] = did_rescale
    cfg["_has_disk"] = bool(disk_regions)
    return cfg


def _clean(cfg):
    """去掉内部状态和 regions_base，得到可以写盘的字典"""
    out = {}
    for k, v in cfg.items():
        if k.startswith("_") or k == "regions_base":
            continue
        out[k] = v
    out["version"] = CONFIG_VERSION
    return out


def save(cfg, path=CONFIG_PATH, preserve_regions=True, partial=False):
    """
    写 config.json。

    preserve_regions=True（默认，主程序用）：只更新 options 之类的设置，
        regions / screen 沿用磁盘上的原值 —— 保证「换分辨率后跑主程序、
        顺便改了抢购目标」不会把磁盘上的坐标改掉。
    preserve_regions=False（标定程序用）：regions 和 screen 都写成当前值。
    partial=True：这份 config 里有项不是「当前屏幕下真实框选」的
        （旧屏幕没重标 / 从没框过用了默认值），写一个 partial 标记，主程序读到会继续提醒重标。
    """
    cur = screen_info()
    out = _clean(cfg)
    out.pop("partial", None)

    if preserve_regions and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                disk = json.load(f) or {}
            if isinstance(disk, dict):
                if disk.get("regions"):
                    out["regions"] = disk["regions"]
                    # v1 旧文件没有 screen 字段：它按基准分辨率解释，这里也照写基准，
                    # 不能写成「当前屏幕」—— 那会让磁盘上的坐标被标上错误的分辨率。
                    out["screen"] = disk.get("screen") or {"w": BASE_W, "h": BASE_H, "dpi": 0}
                if disk.get("calibrated_at"):
                    out["calibrated_at"] = disk["calibrated_at"]
        except Exception:
            pass
    else:
        out["regions"] = {spec["key"]: cfg["regions"][spec["key"]]
                          for spec in SPEC if spec["key"] in cfg.get("regions", {})} or out.get("regions", {})
        out["screen"] = screen_brief(cur)

    if partial:
        out["partial"] = True

    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return path


def is_calibrated(path=CONFIG_PATH):
    """config.json 存在且 16 个位置齐全，才算完成初始化"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            disk = json.load(f)
        reg = disk.get("regions", {})
        return all(k in reg for k in KEYS)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 取坐标（主程序用）
# ---------------------------------------------------------------------------
def box(cfg, key):
    """取矩形：返回 (x1, y1, x2, y2)"""
    b = cfg["regions"][key]
    x1, y1, x2, y2 = b
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def center_of(cfg, key):
    x1, y1, x2, y2 = box(cfg, key)
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def drag_points(cfg, key):
    """取拖拽起止点：返回 ((sx, sy), (ex, ey))"""
    return tuple(tuple(p) for p in cfg["regions"][key])
