# -*- coding: utf-8 -*-
r"""
自动化点击主程序
================
基于 config.json 里标定好的 16 个位置，跑「检测 -> 分流 -> 执行」的闭环。

流程总览
--------
                 ┌──────────────┐
                 │  检测：读 3  │  右上角余额
                 └──────┬───────┘
          ┌─────────────┼──────────────┬─────────────┐
     余额 < 30w    30w ~ 80w      > 80w / 出现 K
          │             │              │
      ┌───▼───┐    ┌────▼────┐    ┌────▼────┐
      │ A 卖货│    │ B 抢购  │    │ 报错暂停 │
      └───┬───┘    └────┬────┘    └─────────┘
          └──────┬──────┘
                 ▼
            回到检测

A 程序（余额不足，卖「空白存储」补钱）
    点 2 仓库页签 -> 在 4 区域找物品图 -> 点 5 购物车 -> 点物品图
    -> 点 6 出售 -> 点 7 确认 -> 7 次 ESC -> 点 16 回初始页

B 程序（余额够，去刷价抢购）
    点 1 配装 -> 点 8/9 槽位（看 options.target）-> 拖 10 滚动条到底
    -> 点 12、点 11、点 12 热身 -> 高速 OCR(13) + 往返点 11/12，
    价格进 20w~30w 立刻停手 -> 点 12 确认 -> 快速移到 14 停 10ms 点
    -> 快速移到 15 停 10ms 点 -> 7 次 ESC -> 点 16 -> 读 3 记余额变化

安全
----
* 默认 **dry-run**：只截图、只判断、只打印，不真的点击。加 --live 才动手。
* F9 是急停键，任何时刻按下都会在下一个动作边界停下。
* 余额 > 80w 或读数出现无法识别的字符（比如 "1.2K" 里的 K）直接 Fatal 暂停。

用法
----
python dvc_auto.py --selftest         # 只体检，不点击
python dvc_auto.py --probe            # 读一次余额，看 OCR 准不准
python dvc_auto.py --find             # 在仓库区找物品图，看分数
python dvc_auto.py --ocr-live         # 持续打印余额/价格 OCR
python dvc_auto.py --learn balance    # 采余额数字字模
python dvc_auto.py --learn price      # 采价格数字字模
python dvc_auto.py                    # 空跑整个闭环（不点击）
python dvc_auto.py --live             # 真跑
"""
import argparse
import json
import os
import random
import sys
import threading
import time
import traceback

if getattr(sys, "frozen", False):
    # 打包成 exe 时，字模/模板/config 都在 exe 旁边的 auto 目录里
    BASE = os.path.join(os.path.dirname(sys.executable), "auto")
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import dvc_deps

dvc_deps.require(__file__)

import numpy as np

import dvc_vision as V
import dvc_win as W

ASSET_DIR = os.path.join(BASE, "assets")
DIGIT_DIR = os.path.join(BASE, "digits")
DEBUG_DIR = os.path.join(BASE, "debug")
LOG_PATH = os.path.join(BASE, "run.log")
BUY_LOG = os.path.join(BASE, "purchase_log.jsonl")

ITEM_TEMPLATE = os.path.join(ASSET_DIR, "blank_storage.png")

VK_F9 = 0x78

DEFAULTS = {
    "step_delay": 0.32,         # 普通点击之间的等待
    "ui_wait": 0.65,            # 切页面 / 切槽位之后的等待
    "esc_gap": 0.18,            # 连按 ESC 的间隔
    "esc_quiet": 1.00,          # 按第一个 ESC 之前先静置多久：等游戏收下上一次点击
    "reset_first": True,        # 开跑前先 7 次 ESC + 点屏幕中央，把界面按回主状态
    "goal_helmet": 1,           # 这一轮抢几件头盔
    "goal_armor": 1,            # 头盔抢够之后抢几件护甲
    "price_min": None,          # 抢购价格下限；None = 用 config.json 里的
    "price_max": None,          # 抢购价格上限；None = 用 config.json 里
    "rapid_click_gap": 0.025,   # 高速往返点击 11/12 的间隔
    "rapid_timeout": 0.0,       # 高速循环最长跑多久；0 = 不限，一直刷到进区间
    "rapid_pre_sleep": 0.012,   # OCR 前等一下，让画面跟上这次点击
    "buy_move_wait": 0.010,     # 光标移到购买按钮后停 10ms
    "hit_settle": 0.030,       # 价格命中后点回刷价点2，再等 30ms 才买
    "buy_only": False,        # 只抢购：不读余额、不跑 A 程序
    "click_hold": 0.035,
    "click_move_wait": 0.080,   # 鼠标挪到目标后停多久再按下：太短游戏会当成没点中
    "double_click_gap": 0.09,   # 双击两次之间的间隔：太大会被当成两次单击
    "sell_ok_delay": 0.80,      # 点完「出售」到点「确认」之间等多久：弹窗出来要时间
    "find_threshold": 0.72,     # 找物品图的 NCC 阈值
    "find_inset": 0.10,         # 找图时把模板四周裁掉 10%，躲开选中态边框
    "ocr_min_score": 0.55,
}


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
_log_file = None


def log(msg, level="INFO"):
    global _log_file
    line = "[%s] %-5s %s" % (time.strftime("%H:%M:%S"), level, msg)
    print(line, flush=True)
    try:
        if _log_file is None:
            _log_file = open(LOG_PATH, "a", encoding="utf-8")
        _log_file.write(line + "\n")
        _log_file.flush()
    except Exception:
        pass


def beep(freq=880, ms=180, times=1):
    try:
        import winsound
        for _ in range(times):
            winsound.Beep(freq, ms)
    except Exception:
        pass


class Fatal(Exception):
    """需要人工介入：余额越界、找不到目标物品等"""


class Halt(Exception):
    """用户按了 F9"""


# ---------------------------------------------------------------------------
# 坐标工具
# ---------------------------------------------------------------------------
def box_of(regions, key):
    b = regions[key]
    x1, y1, x2, y2 = b
    return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]


def pick_in(regions, key, inset=0.22):
    """在区域里随机取一个点。

    固定坐标点同一个像素太机械，而且游戏里有些控件对边缘不敏感、
    对中心敏感（比如选中项会吞掉点击），所以在框内随机落点，
    同时按 inset 比例躲开四边。
    """
    x1, y1, x2, y2 = box_of(regions, key)
    w, h = x2 - x1, y2 - y1
    mx = max(1, int(w * inset))
    my = max(1, int(h * inset))
    if x2 - mx <= x1 + mx:
        mx = 0
    if y2 - my <= y1 + my:
        my = 0
    return random.randint(x1 + mx, x2 - mx), random.randint(y1 + my, y2 - my)


def center_of(regions, key):
    b = box_of(regions, key)
    return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)


def drag_of(regions, key):
    v = regions[key]
    return (int(v[0][0]), int(v[0][1])), (int(v[1][0]), int(v[1][1]))


# ---------------------------------------------------------------------------
# 主控
# ---------------------------------------------------------------------------
class Auto:
    def __init__(self, cfg, opts, live=False):
        self.cfg = cfg
        self.regions = cfg["regions"]
        self.options = cfg.get("options") or {}
        self.opts = dict(DEFAULTS)
        self.opts.update(opts or {})
        self.live = bool(live)

        self.screen = W.screen_info()
        self.grabber = W.Grabber()
        self.input = W.Input(self.screen)
        self.ocr = V.DigitOCR(DIGIT_DIR, min_score=self.opts["ocr_min_score"])
        self.item_tpl = self._load_item_template()

        self.halt = False
        self.clock_start = time.time()
        self.last_balance = None
        self.rounds = 0
        self.sold = 0
        self.bought = 0
        # 这一轮要抢多少件：先头盔后护甲，抢够一个换下一个
        self.goals = {"helmet": int(self.opts.get("goal_helmet", 1)),
                      "armor": int(self.opts.get("goal_armor", 1))}
        # 界面/命令行给的价格区间优先，没给就用 config.json 里的
        self.price_lo = int(self.opts.get("price_min")
                            or self.options.get("price_min", 200000))
        self.price_hi = int(self.opts.get("price_max")
                            or self.options.get("price_max", 300000))
        self.done = {"helmet": 0, "armor": 0}

    # ---------------- 基础 ----------------
    def _load_item_template(self):
        try:
            from PIL import Image
            a = np.asarray(Image.open(ITEM_TEMPLATE).convert("RGB"), dtype=np.uint8)
            log("物品模板 %dx%d 已载入" % (a.shape[1], a.shape[0]))
            return a
        except Exception as e:
            log("物品模板载入失败: %s" % e, "WARN")
            return None

    def check_halt(self):
        if W.key_down(VK_F9):
            self.halt = True
        if self.halt:
            raise Halt()

    def wait(self, sec=None):
        """可被 F9 打断的等待"""
        sec = self.opts["step_delay"] if sec is None else sec
        end = time.perf_counter() + max(0.0, sec)
        while time.perf_counter() < end:
            self.check_halt()
            time.sleep(0.008)

    def grab(self, key):
        return self.grabber.grab_box(box_of(self.regions, key))

    def click_point(self, x, y, hold=None, double=False):
        self.check_halt()
        hold = self.opts["click_hold"] if hold is None else hold
        if double:
            log("双击 (%d, %d)" % (x, y), "ACT")
        elif hold >= 0.2:
            log("长按 (%d, %d) %.2f 秒" % (x, y, hold), "ACT")
        else:
            log("点击 (%d, %d)" % (x, y), "ACT")
        if self.live:
            self.input.click_at(x, y, hold=hold,
                                move_wait=self.opts["click_move_wait"],
                                double=double,
                                gap=self.opts["double_click_gap"])
        else:
            self.input.move_to(x, y)

    def click_key(self, key, hold=None, double=False):
        x, y = pick_in(self.regions, key)
        self.click_point(x, y, hold=hold, double=double)

    def drag_key(self, key):
        self.check_halt()
        p1, p2 = drag_of(self.regions, key)
        log("拖拽 %r -> %r" % (p1, p2), "ACT")
        if self.live:
            self.input.drag(p1, p2)
        else:
            self.input.move_to(*p1)

    def press_escape(self, times=7):
        self.check_halt()
        quiet = self.opts.get("esc_quiet", 0.0)
        if quiet > 0:
            # 鼠标刚抬起就发 ESC，游戏常把这次点击当成没完成。先让画面静一会儿。
            log("静置 %.2f 秒，等游戏收下上一次点击" % quiet, "ACT")
            self.wait(quiet)
        log("按 %d 次 ESC" % times, "ACT")
        if self.live:
            self.input.press_escape(times, gap=self.opts["esc_gap"])
        self.wait(self.opts["esc_gap"] * times)

    def reset_ui(self):
        """把界面按回主状态：点一下屏幕中央 → 连按 7 次 ESC → 再点一次屏幕中央。

        run() 开头默认走一次。上一次可能停在仓库、配装或者出售弹窗上，
        不重置就直接读余额，读数没问题，但接下来那一下点击会点在错的界面上。
        先点一下是为了让鼠标落进游戏窗口（键盘事件才收得到），
        最后再点一下把焦点从可能残留的弹窗上收回来。"""
        log("重置界面：点击屏幕中央 → 7 次 ESC → 再点一次", "STEP")
        self.click_key("click_center")
        self.press_escape(7)
        self.click_key("click_center")
        self.wait(self.opts["ui_wait"])

    # ---------------- 识别 ----------------
    def read_balance(self):
        return self.ocr.read(self.grab("ocr_balance"))

    def read_price(self):
        return self.ocr.read(self.grab("ocr_price"))

    def find_item(self, retries=2):
        """在仓库中间装备区里找「空白存储」，返回 (x, y, score) 或 None"""
        if self.item_tpl is None:
            raise Fatal("没有物品模板图 %s" % ITEM_TEMPLATE)
        for i in range(max(1, retries)):
            view = self.grab("scan_blank")
            r = V.find_template_multi(view, self.item_tpl,
                                      threshold=self.opts["find_threshold"],
                                      inset=self.opts["find_inset"])
            if r:
                x, y, score, _ = r
                b = box_of(self.regions, "scan_blank")
                cx = b[0] + x + self.item_tpl.shape[1] // 2
                cy = b[1] + y + self.item_tpl.shape[0] // 2
                return cx, cy, score
            if i != retries - 1:
                log("仓库区没找到物品图，等画面刷新后重试", "WARN")
                self.wait(0.4)
        return None

    # ---------------- 检测 ----------------
    def probe(self, retries=5):
        """
        读右上角余额，决定走哪条路。
        返回 ("A"|"B", 余额) 或抛 Fatal。
        """
        last = None
        for _ in range(retries):
            self.check_halt()
            r = self.read_balance()
            last = r
            txt = (r.get("text") or "").upper()
            # 余额显示成 3,160K 这种带单位的，按需求必须直接停，不能猜
            if "K" in txt or "W" in txt or "M" in txt:
                raise Fatal("余额读数带单位（%r），超出正常显示范围，按要求暂停" % txt)
            if r["ok"] and r["value"] is not None:
                v = int(r["value"])
                # 画面花掉时可能只切出半边数字（比如 640350 认成 640），
                # 这种明显不合理的值当没读到，重试
                if v < 1000 or len(str(v)) < 5:
                    log("读到 %d（%d 位），明显不完整，重试" % (v, len(str(v))), "WARN")
                    time.sleep(0.25)
                    continue
                self.last_balance = v
                lo = int(self.options.get("balance_low", 300000))
                hi = int(self.options.get("balance_high", 800000))
                log("余额 %d（原始文本 %r，置信 %.2f）" % (v, r["text"], r["conf"]))
                if v > hi:
                    raise Fatal("余额 %d 已经高于上限 %d，按要求暂停" % (v, hi))
                if v < lo:
                    return "A", v
                return "B", v
            time.sleep(0.25)

        raise Fatal("余额读不出来（最后一次文本 %r，置信 %.2f）。"
                    "可能是字模没采、框偏了，或者余额变成了带 K/W 的单位"
                    % (last["text"] if last else "", last["conf"] if last else 0.0))

    # ---------------- A 程序 ----------------
    def program_a(self):
        log("===== A 程序：卖「空白存储」补余额 =====", "STEP")
        self.click_key("tab_cangku")
        self.wait(self.opts["ui_wait"])

        hit = self.find_item()
        if not hit:
            raise Fatal("仓库中间区没找到「空白存储」物品图，先跑 --find 调阈值")
        ix, iy, score = hit
        log("找到物品，屏幕坐标 (%d, %d)，匹配分 %.3f" % (ix, iy, score))

        self.click_key("btn_cart")
        self.wait(self.opts["ui_wait"])
        self.click_point(ix, iy)
        self.wait(self.opts["ui_wait"])
        self.click_key("btn_sell")
        self.wait(self.opts["sell_ok_delay"])
        self.click_key("btn_sell_ok", double=True)
        self.wait(self.opts["ui_wait"])

        self.press_escape(7)
        self.click_key("click_center")
        self.wait(self.opts["ui_wait"])
        self.sold += 1
        log("A 程序完成，累计卖出 %d 件" % self.sold, "STEP")

    # ---------------- 抢购计划 ----------------
    def pick_target(self):
        """先头盔后护甲，按还没抢够的顺序挑；都够了返回 None。"""
        for kind in ("helmet", "armor"):
            if self.done.get(kind, 0) < self.goals.get(kind, 0):
                return kind
        return None

    def goal_text(self):
        return "头盔 %d/%d、护甲 %d/%d" % (
            self.done["helmet"], self.goals["helmet"],
            self.done["armor"], self.goals["armor"])

    # ---------------- B 程序 ----------------
    def program_b(self, kind="helmet"):
        name = "护甲" if kind == "armor" else "头盔"
        log("===== B 程序：抢%s（%s）=====" % (name, self.goal_text()), "STEP")
        slot_key = "slot_armor" if kind == "armor" else "slot_helmet"

        self.click_key("btn_peizhuang")
        self.wait(self.opts["ui_wait"])
        self.click_key(slot_key)
        self.wait(self.opts["ui_wait"])
        self.drag_key("drag_scroll")
        self.wait(self.opts["ui_wait"])

        # 热身：12 -> 11 -> 12
        self.click_key("click_b")
        self.wait(self.opts["rapid_click_gap"])
        self.click_key("click_a")
        self.wait(self.opts["rapid_click_gap"])
        self.click_key("click_b")
        self.wait(self.opts["rapid_click_gap"])

        hit = self.rapid_hunt()
        if hit is None:
            raise Fatal("高速循环意外结束，没抓到 %d~%d 的价格"
                        % (self.price_lo, self.price_hi))
        log("价格命中 %d，停止点击" % hit, "STEP")

        self.click_key("click_b")
        # 点回刷价点2之后让画面稳一下再买：刚停止连点时那一格的报价还在刷新，
        # 立刻点购买容易点空或者买到上一档的价格
        settle = float(self.opts["hit_settle"])
        if settle > 0:
            log("点回刷价点2，停 %.0fms" % (settle * 1000), "ACT")
            self.wait(settle)

        before = self.last_balance
        # 15 / 14 / 15 / 14 来回点：购买 2 和购买 1 两个按钮轮着按，
        # 哪个是真的那个都能吃上（两个按钮长相接近，只靠一次容易点空）
        for key in ("btn_buy_2", "btn_buy_1", "btn_buy_2", "btn_buy_1"):
            self.fast_click(key)

        self.press_escape(7)
        self.click_key("click_center")
        self.wait(self.opts["ui_wait"])

        after = self.read_balance()
        spend = None
        if before is not None and after["ok"] and after["value"] is not None:
            spend = int(before) - int(after["value"])
            log("余额 %s -> %s，本次花费 %s" % (before, after["value"], spend), "ACT")
        else:
            log("买后余额读取不稳: %r" % (after["text"],), "WARN")
        self._record_purchase(hit, before, after.get("value"), spend)

        # 余额掉了才算真买到：手慢了或者价格被抢走，余额是不动的
        if spend is not None and spend > 0:
            self.bought += 1
            self.done[kind] = self.done.get(kind, 0) + 1
            # 给界面的统计通道，裸行不进日志
            print("PURCHASE %s %d" % (kind, hit), flush=True)
            log("买到%s，花费 %d，%s" % (name, spend, self.goal_text()), "STEP")
        else:
            log("%s 没买到（余额没变化），继续下一轮" % name, "WARN")
            return None
        return hit

    def fast_click(self, key):
        """光标快速移动 -> 停 10ms -> 点击"""
        self.check_halt()
        x, y = pick_in(self.regions, key)
        log("快速点名 (%d, %d) 停 %.0fms" % (x, y, self.opts["buy_move_wait"] * 1000), "ACT")
        if self.live:
            self.input.move_to(x, y)
            W.precise_sleep(self.opts["buy_move_wait"])
            self.input.click_at(x, y, hold=self.opts["click_hold"], move_first=False)

    def rapid_hunt(self):
        """
        高速 OCR + 往返点击。

        点击线程反复点 11 和 12（切换两行的选中态，逼游戏刷新最低行报价），
        OCR 线程盯着 13 区域。价格落进 [price_min, price_max] 就停手。
        OCR 比点击快得多，所以每轮先睡半个刷新周期，避免读到上一次的画面。
        """
        p_lo, p_hi = self.price_lo, self.price_hi
        gap = self.opts["rapid_click_gap"]
        _to = float(self.opts["rapid_timeout"] or 0)
        deadline = (time.perf_counter() + _to) if _to > 0 else None

        stop = threading.Event()
        hit = {}
        stats = {"clicks": 0, "reads": 0, "unread": 0}
        # 价格每变一次就往外吐一行 PRICE，给界面上的价格卡用；限流到 150ms 一次，
        # 免得一秒几十行把管道和浏览器都塞满
        last_price = [0.0]
        errors = []

        b11 = box_of(self.regions, "click_a")
        b12 = box_of(self.regions, "click_b")
        def rnd(box):
            """在框里随机落点，躲开四边 22%"""
            bw, bh = box[2] - box[0], box[3] - box[1]
            mx, my = max(1, int(bw * 0.22)), max(1, int(bh * 0.22))
            if box[2] - mx <= box[0] + mx:
                mx = 0
            if box[3] - my <= box[1] + my:
                my = 0
            return (random.randint(box[0] + mx, box[2] - mx),
                    random.randint(box[1] + my, box[3] - my))

        price_box = box_of(self.regions, "ocr_price")

        def clicker():
            try:
                while not stop.is_set():
                    # 兜底：reader 万一异常没设 stop，这里也不能无限点下去
                    if deadline is not None and time.perf_counter() > deadline + 3.0:
                        log("点击线程超出硬超时，强制停止", "WARN")
                        stop.set()
                        return
                    if W.key_down(VK_F9):
                        self.halt = True
                        stop.set()
                        return
                    p = rnd(b11)
                    self.input.click_at(p[0], p[1], hold=self.opts["click_hold"])
                    stats["clicks"] += 1
                    if stop.is_set():
                        return
                    time.sleep(gap)
                    p = rnd(b12)
                    self.input.click_at(p[0], p[1], hold=self.opts["click_hold"])
                    stats["clicks"] += 1
                    time.sleep(gap)
            except Exception as e:      # noqa: BLE001
                errors.append(repr(e))
                stop.set()

        def reader():
            try:
                while not stop.is_set():
                    # 超时必须由 reader 负责设 stop，否则点击线程会一直转到天荒地老
                    if deadline is not None and time.perf_counter() >= deadline:
                        log("高速循环到了 %.0f 秒还没进区间，停止点击"
                            % self.opts["rapid_timeout"], "WARN")
                        stop.set()
                        return
                    time.sleep(self.opts["rapid_pre_sleep"])
                    if stop.is_set():
                        return
                    img = self.grabber.grab_box(price_box)
                    r = self.ocr.read(img)
                    stats["reads"] += 1
                    if r["ok"]:
                        v = int(r["value"])
                        now = time.perf_counter()
                        if now - last_price[0] >= 0.15:
                            last_price[0] = now
                            print("PRICE %d" % v, flush=True)
                        if p_lo <= v <= p_hi:
                            hit["value"] = v
                            hit["text"] = r["text"]
                            stop.set()
                            return
                    else:
                        stats["unread"] += 1
            except Exception as e:      # noqa: BLE001
                errors.append(repr(e))
                stop.set()

        if not self.live:
            log("dry-run：高速循环不点击，只读一次价格", "WARN")
            r = self.read_price()
            log("当前价格读数 %r" % (r,))
            # 返回非 None，让空跑能把 B 程序后面的步骤也走一遍
            return r["value"] if r.get("ok") else 0

        log("进入高速循环（区间 %d~%d，点击间隔 %.0fms）"
            % (p_lo, p_hi, gap * 1000), "STEP")
        t1 = threading.Thread(target=clicker, daemon=True)
        t2 = threading.Thread(target=reader, daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        stop.set()

        log("高速循环结束：点击 %d 次，读价 %d 次（认不出 %d 次）"
            % (stats["clicks"], stats["reads"], stats["unread"]))
        if errors:
            log("线程异常: %s" % "; ".join(errors), "WARN")
        if self.halt:
            raise Halt()
        return hit.get("value")

    # ---------------- 记录 ----------------
    def _record_purchase(self, price, before, after, spend):
        if not self.live:
            return
        rec = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "price_seen": price,
            "balance_before": before,
            "balance_after": after,
            "spend": spend,
        }
        try:
            with open(BUY_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---------------- 主循环 ----------------
    def run(self, max_rounds=0):
        log("启动：%dx%d @ %d DPI，%s%s"
            % (self.screen["w"], self.screen["h"], self.screen["dpi"],
               "实盘点击" if self.live else "dry-run 空跑",
               "，只抢购模式" if self.opts.get("buy_only") else ""))
        if self.screen["vx"] or self.screen["vy"]:
            log("主屏原点不是 (0,0)，多显示器下坐标可能对不上，建议只用主屏", "WARN")
        log("当前前台窗口：%s" % (W.foreground_title() or "(空)"))

        if not self.ocr.ocr_ready():
            raise Fatal("数字字模不全（%d 张）。先跑 --learn balance 和 --learn price"
                        % len(self.ocr.model))

        # 开跑前先归零：上一轮可能停在仓库、配装或者出售弹窗上
        if self.opts.get("reset_first", True):
            self.reset_ui()

        while True:
            self.check_halt()
            self.rounds += 1
            if max_rounds and self.rounds > max_rounds:
                log("到达轮数上限 %d，退出" % max_rounds)
                return
            log("---------- 第 %d 轮 ----------" % self.rounds, "STEP")
            try:
                if self.opts.get("buy_only"):
                    # 只抢购模式：不读余额、不跑 A，直接进 B
                    kind = self.pick_target()
                    if kind is None:
                        log("头盔 %d/%d、护甲 %d/%d 都抢够了，收工"
                            % (self.done["helmet"], self.goals["helmet"],
                               self.done["armor"], self.goals["armor"]), "STEP")
                        return
                    self.program_b(kind)
                else:
                    act, bal = self.probe()
                    if act == "A":
                        self.program_a()
                    else:
                        kind = self.pick_target()
                        if kind is None:
                            log("头盔 %d/%d、护甲 %d/%d 都抢够了，收工"
                                % (self.done["helmet"], self.goals["helmet"],
                                   self.done["armor"], self.goals["armor"]), "STEP")
                            return
                        self.program_b(kind)
            except Fatal:
                raise
            except Halt:
                raise
            except Exception as e:      # noqa: BLE001
                log("本轮执行出错: %r" % e, "ERR")
                log(traceback.format_exc().strip().splitlines()[-1], "ERR")
                self.wait(1.0)


# ---------------------------------------------------------------------------
# 自检与调试命令
# ---------------------------------------------------------------------------
def resolve_config(explicit=None):
    cands = []
    if explicit:
        cands.append(explicit)
    cands += [
        os.path.join(BASE, "config.json"),
        os.path.join(os.path.dirname(BASE), "config.json"),
        os.path.join(os.path.dirname(BASE), "src", "标定程序", "config.json"),
    ]
    for p in cands:
        if p and os.path.exists(p):
            return os.path.abspath(p)
    raise FileNotFoundError("找不到 config.json，用 --config 指定")


def load_cfg(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    reg = cfg.get("regions") or {}
    missing = [k for k in ("btn_peizhuang", "tab_cangku", "ocr_balance", "scan_blank")
               if k not in reg]
    if missing:
        raise ValueError("config.json 缺关键项: %s" % ", ".join(missing))
    return cfg


def cmd_selftest(cfg, cfg_path, opts):
    print("=" * 62)
    print(" 自检")
    print("=" * 62)
    s = W.screen_info()
    print("DPI 模式     :", W.ensure_dpi_aware())
    print("主屏         : %dx%d @ %d%%" % (s["w"], s["h"], int(round(s["dpi"] / 96.0 * 100))))
    print("虚拟桌面     : %dx%d at (%d,%d)" % (s["vw"], s["vh"], s["vx"], s["vy"]))
    print("配置         :", cfg_path)

    scr = cfg.get("screen") or {}
    if (scr.get("w"), scr.get("h")) != (s["w"], s["h"]):
        print(" !! 配置标定在 %sx%s，当前屏幕 %dx%d —— 坐标会整体错位"
              % (scr.get("w"), scr.get("h"), s["w"], s["h"]))
    if (scr.get("dpi") or 0) and abs(int(scr["dpi"]) - s["dpi"]) > 2:
        print(" !! 配置标定在 %s DPI，当前 %d DPI" % (scr["dpi"], s["dpi"]))

    reg = cfg["regions"]
    print("\n16 个位置：")
    for k in ("btn_peizhuang", "tab_cangku", "ocr_balance", "scan_blank", "btn_cart",
              "btn_sell", "btn_sell_ok", "slot_helmet", "slot_armor", "drag_scroll",
              "click_a", "click_b", "ocr_price", "btn_buy_1", "btn_buy_2", "click_center"):
        if k not in reg:
            print("  ! %-14s 缺失" % k)
            continue
        if len(reg[k]) == 2 and isinstance(reg[k][0], (list, tuple)):
            print("  %-14s drag  %s -> %s" % (k, reg[k][0], reg[k][1]))
        else:
            b = box_of(reg, k)
            c = center_of(reg, k)
            print("  %-14s box   %-22s 中心 %s  %dx%d"
                  % (k, b, c, b[2] - b[0], b[3] - b[1]))

    print("\n截图检查：")
    g = W.Grabber()
    full = g.grab(0, 0, s["w"], s["h"])
    flat = V.is_flat(full, tol=1.0)
    print("  全屏抓取     :", "全黑/全同色 —— 独占全屏下 BitBlt 抓不到，改成无边框窗口"
          if flat else "正常，%dx%d" % (full.shape[1], full.shape[0]))
    V.save_rgb(os.path.join(DEBUG_DIR, "selftest_full.png"), full)
    for k in ("ocr_balance", "ocr_price", "scan_blank"):
        if k in reg:
            img = g.grab_box(box_of(reg, k))
            p = os.path.join(DEBUG_DIR, "selftest_%s.png" % k)
            V.save_rgb(p, img)
            print("  %-14s : %dx%d -> %s" % (k, img.shape[1], img.shape[0], p))

    print("\nOCR 字模：")
    ocr = V.DigitOCR(DIGIT_DIR, min_score=opts["ocr_min_score"])
    have = "".join(sorted(ocr.model.keys()))
    print("  已采集       :", have or "(空)")
    print("  可用         :", "是" if ocr.ocr_ready() else "否 —— 先跑 --learn balance / --learn price")
    for k in ("ocr_balance", "ocr_price"):
        if k not in reg:
            continue
        img = g.grab_box(box_of(reg, k))
        r = ocr.read(img)
        print("  %-14s : %r ok=%s conf=%.2f" % (k, r["text"], r["ok"], r["conf"]))

    print("\n找物品图：")
    try:
        from PIL import Image
        tpl = np.asarray(Image.open(ITEM_TEMPLATE).convert("RGB"), dtype=np.uint8)
        print("  模板         : %s %dx%d" % (ITEM_TEMPLATE, tpl.shape[1], tpl.shape[0]))
        if "scan_blank" in reg:
            view = g.grab_box(box_of(reg, "scan_blank"))
            r = V.find_template_multi(view, tpl, threshold=opts["find_threshold"],
                                      inset=opts["find_inset"])
            if r:
                print("  匹配         : 偏移(%d,%d) 分数 %.3f" % (r[0], r[1], r[2]))
            else:
                print("  匹配         : 没找到（游戏没开或不在仓库页时正常）")
    except Exception as e:      # noqa: BLE001
        print("  跳过:", e)

    g.close()
    print("\n自检结束。要真跑：python dvc_auto.py --live\n")


def cmd_dump(cfg, key):
    g = W.Grabber()
    reg = cfg["regions"]
    if key not in reg:
        print("没有这个 key:", key)
        return 1
    img = g.grab_box(box_of(reg, key))
    p = os.path.join(DEBUG_DIR, "%s.png" % key)
    V.save_rgb(p, img)
    print("已保存", p, img.shape[1], "x", img.shape[0])
    g.close()
    return 0


def cmd_learn(cfg, which, opts):
    """
    采字模。

    关键点：截图必须发生在游戏处于前台的时候。如果做成「截一张 -> 等用户输入
    -> 再截一张」，用户输入时控制台在前台，下一张抓到的就是控制台本身。
    所以这里先倒计时让用户切回游戏，一口气连抓几张，再逐张核对输入。
    """
    g = W.Grabber()
    reg = cfg["regions"]
    key = "ocr_balance" if which == "balance" else "ocr_price"
    if key not in reg:
        print("config 里没有", key)
        return 1
    ocr = V.DigitOCR(DIGIT_DIR, min_score=opts["ocr_min_score"])
    shots = 3 if which == "balance" else 6

    print("=" * 62)
    print(" 学习 %s 区域（%s）的数字字模" % (which, key))
    print("=" * 62)
    print(" 每次连抓 %d 张。截图时必须让【游戏在最前面】，" % shots)
    print(" 所以流程是：按回车 -> 5 秒倒计时（你切回游戏）-> 自动连抓。")
    print()

    round_no = 0
    while True:
        try:
            input(" 把游戏切到最前、让数字露出来，然后按回车开始：")
        except (EOFError, KeyboardInterrupt):
            break
        for i in range(5, 0, -1):
            print("\r 倒计时 %d ... 切回游戏！" % i, end="", flush=True)
            time.sleep(1.0)
        print("\r 正在抓取...          ")

        paths = []
        for i in range(shots):
            img = g.grab_box(box_of(reg, key))
            p = os.path.join(DEBUG_DIR, "learn_%s_r%d_%02d.png" % (which, round_no, i))
            V.save_rgb(p, img)
            paths.append(p)
            W.precise_sleep(0.45)
        print(" 抓好了 %d 张，都在 %s" % (len(paths), DEBUG_DIR))
        print(" 逐张打开看，把图上的数字原样输进去（不要逗号），回车跳过。\n")

        for p in paths:
            segs = ocr.segment(_load_rgb(p))
            print("  %s   切出 %d 个字符" % (os.path.basename(p), len(segs)))
            try:
                txt = input("    图上数字（回车跳过，q 结束）: ").strip()
            except (EOFError, KeyboardInterrupt):
                txt = "q"
            if txt.lower() == "q":
                g.close()
                _report_digits(ocr)
                return 0
            if not txt:
                continue
            try:
                saved = ocr.learn(_load_rgb(p), txt)
                print("    写入 %d 张字模" % len(saved))
            except ValueError as e:
                print("    失败:", e)
        round_no += 1
        _report_digits(ocr)
        try:
            again = input("\n 再来一轮？(y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if again != "y":
            break

    g.close()
    _report_digits(ocr)
    return 0


def _load_rgb(path):
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)


def _report_digits(ocr):
    have = "".join(sorted(ocr.model.keys()))
    miss = [str(d) for d in range(10) if str(d) not in ocr.model]
    print("\n 当前字模: %s" % (have or "(空)"))
    if miss:
        print(" 还缺    : %s" % " ".join(miss))
    else:
        print(" 0-9 齐了，可以识别了")


def cmd_learn_from(path, text, opts):
    """从一张已有的截图学字模（不想来回切窗口时用这个）"""
    if not os.path.exists(path):
        print("找不到图片:", path)
        return 1
    ocr = V.DigitOCR(DIGIT_DIR, min_score=opts["ocr_min_score"])
    rgb = _load_rgb(path)
    segs = ocr.segment(rgb)
    print("%s  %dx%d  切出 %d 个字符" % (path, rgb.shape[1], rgb.shape[0], len(segs)))
    try:
        saved = ocr.learn(rgb, text)
        print("写入 %d 张字模" % len(saved))
    except ValueError as e:
        print("失败:", e)
        return 1
    _report_digits(ocr)
    return 0


def cmd_ocr_live(cfg, opts, seconds=30):
    g = W.Grabber()
    ocr = V.DigitOCR(DIGIT_DIR, min_score=opts["ocr_min_score"])
    reg = cfg["regions"]
    print("持续读 OCR，%.0f 秒后自动结束（Ctrl+C 提前退出）" % seconds)
    end = time.perf_counter() + seconds
    try:
        while time.perf_counter() < end:
            rb = ocr.read(g.grab_box(box_of(reg, "ocr_balance")))
            rp = ocr.read(g.grab_box(box_of(reg, "ocr_price")))
            print("[%s] 余额 %-10r ok=%-5s | 价格 %-10r ok=%s"
                  % (time.strftime("%H:%M:%S"), rb["text"], rb["ok"], rp["text"], rp["ok"]))
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    g.close()
    return 0


def cmd_find(cfg, opts, repeat=1):
    from PIL import Image
    tpl = np.asarray(Image.open(ITEM_TEMPLATE).convert("RGB"), dtype=np.uint8)
    g = W.Grabber()
    reg = cfg["regions"]
    for i in range(repeat):
        view = g.grab_box(box_of(reg, "scan_blank"))
        t0 = time.perf_counter()
        r = V.find_template_multi(view, tpl, threshold=opts["find_threshold"],
                                  inset=opts["find_inset"])
        dt = (time.perf_counter() - t0) * 1000
        if r:
            print("命中 偏移(%d,%d) 分数 %.3f 尺度 %.2f 用时 %.0fms"
                  % (r[0], r[1], r[2], r[3], dt))
        else:
            print("未命中 用时 %.0fms（阈值 %.2f）" % (dt, opts["find_threshold"]))
        if i == 0:
            V.save_rgb(os.path.join(DEBUG_DIR, "find_view.png"), view)
        time.sleep(0.3)
    g.close()
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="格拉默脑机 · 坐标自动化点击（基于标定程序产出的 config.json）")
    ap.add_argument("--config", help="config.json 路径")
    ap.add_argument("--live", action="store_true", help="真的点击（默认 dry-run 只打印）")
    ap.add_argument("--selftest", action="store_true", help="体检：DPI/坐标/截图/OCR/找图")
    ap.add_argument("--probe", action="store_true", help="只读一次余额")
    ap.add_argument("--find", action="store_true", help="只在仓库区找物品图")
    ap.add_argument("--learn", choices=["balance", "price"], help="采数字字模（交互式连拍）")
    ap.add_argument("--learn-from", nargs=2, metavar=("PNG", "DIGITS"),
                    help="从一张已有截图学字模，例：--learn-from debug/shot.png 250000")
    ap.add_argument("--ocr-live", action="store_true", help="持续打印 OCR 结果")
    ap.add_argument("--dump", metavar="KEY", help="把某个标定区域存成 png")
    ap.add_argument("--rounds", type=int, default=0, help="最多跑几轮，0=不限")
    ap.add_argument("--only", choices=["a", "b"], help="只跑 A 或 B，跳过检测")
    ap.add_argument("--threshold", type=float, help="找图 NCC 阈值，默认 %.2f"
                    % DEFAULTS["find_threshold"])
    ap.add_argument("--step-delay", type=float, help="普通动作间隔秒，默认 %.2f"
                    % DEFAULTS["step_delay"])
    ap.add_argument("--click-gap", type=float, help="高速往返点击间隔秒，默认 %.3f"
                    % DEFAULTS["rapid_click_gap"])
    ap.add_argument("--double-gap", type=float, help="双击两次之间的间隔秒，默认 %.3f"
                    % DEFAULTS["double_click_gap"])
    ap.add_argument("--esc-quiet", type=float, help="按第一个 ESC 前静置多少秒，默认 %.2f"
                    % DEFAULTS["esc_quiet"])
    ap.add_argument("--esc-gap", type=float, help="连按 ESC 的间隔秒，默认 %.2f"
                    % DEFAULTS["esc_gap"])
    ap.add_argument("--move-wait", type=float, help="鼠标挪到目标后停多久才按下，默认 %.3f"
                    % DEFAULTS["click_move_wait"])
    ap.add_argument("--no-reset", action="store_true",
                    help="开跑前不做「7 次 ESC + 点屏幕中央」那套界面重置")
    ap.add_argument("--buy-only", action="store_true",
                        help="只抢购：跳过读余额和 A 程序")
    ap.add_argument("--helmet", type=int, metavar="N",
                    help="这轮抢几件头盔，默认 %d" % DEFAULTS["goal_helmet"])
    ap.add_argument("--armor", type=int, metavar="N",
                    help="头盔抢够之后抢几件护甲，默认 %d" % DEFAULTS["goal_armor"])
    ap.add_argument("--price-min", type=int, metavar="N",
                    help="抢购价格下限，默认用 config.json 里的")
    ap.add_argument("--price-max", type=int, metavar="N",
                    help="抢购价格上限，默认用 config.json 里的")
    args = ap.parse_args(argv)

    opts = dict(DEFAULTS)
    if args.threshold is not None:
        opts["find_threshold"] = args.threshold
    if args.step_delay is not None:
        opts["step_delay"] = args.step_delay
    if args.click_gap is not None:
        opts["rapid_click_gap"] = args.click_gap
    if args.double_gap is not None:
        opts["double_click_gap"] = args.double_gap
    if args.esc_quiet is not None:
        opts["esc_quiet"] = args.esc_quiet
    if args.esc_gap is not None:
        opts["esc_gap"] = args.esc_gap
    if args.move_wait is not None:
        opts["click_move_wait"] = args.move_wait
    if args.no_reset:
        opts["reset_first"] = False
    if args.helmet is not None:
        opts["buy_only"] = bool(args.buy_only)
        opts["goal_helmet"] = max(0, int(args.helmet))
    if args.armor is not None:
        opts["goal_armor"] = max(0, int(args.armor))
    if args.price_min is not None:
        opts["price_min"] = max(0, int(args.price_min))
    if args.price_max is not None:
        opts["price_max"] = max(0, int(args.price_max))

    os.makedirs(DEBUG_DIR, exist_ok=True)
    os.makedirs(DIGIT_DIR, exist_ok=True)

    try:
        cfg_path = resolve_config(args.config)
        cfg = load_cfg(cfg_path)
    except Exception as e:      # noqa: BLE001
        print("读取 config 失败:", e)
        return 2

    if args.selftest:
        cmd_selftest(cfg, cfg_path, opts)
        return 0
    if args.dump:
        return cmd_dump(cfg, args.dump)
    if args.learn_from:
        return cmd_learn_from(args.learn_from[0], args.learn_from[1], opts)
    if args.learn:
        return cmd_learn(cfg, args.learn, opts)
    if args.ocr_live:
        return cmd_ocr_live(cfg, opts)
    if args.find:
        return cmd_find(cfg, opts, repeat=3)
    if args.probe:
        log("只读一次余额（不会点击）")
        a = Auto(cfg, opts, live=False)
        if not a.ocr.ocr_ready():
            log("数字字模不齐（已采：%s）。先跑 --learn balance 和 --learn price"
                % ("".join(sorted(a.ocr.model.keys())) or "空"), "FATAL")
            return 1
        try:
            act, bal = a.probe()
            log("结论: %s（余额 %d）" % ("A 程序" if act == "A" else "B 程序", bal))
        except Fatal as e:
            log(str(e), "FATAL")
            return 1
        return 0

    a = Auto(cfg, opts, live=args.live)
    try:
        if args.only == "a":
            a.program_a()
            return 0
        if args.only == "b":
            if not args.live:
                log("B 程序包含高速点击，dry-run 下只能读一次价格", "WARN")
            a.program_b()
            return 0
        a.run(max_rounds=args.rounds)
    except Halt:
        log("已按 F9 急停", "STOP")
        beep(660, 200, 2)
        return 0
    except Fatal as e:
        log("暂停：%s" % e, "FATAL")
        beep(440, 400, 3)
        if args.live:
            print("\n程序已暂停，人工处理完可按回车退出。")
            try:
                input()
            except Exception:
                pass
        return 1
    except KeyboardInterrupt:
        log("Ctrl+C 退出", "STOP")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
