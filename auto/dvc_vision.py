# -*- coding: utf-8 -*-
r"""
图像识别层
==========
两件事：

1. **模板找图**：在仓库中间装备区里找「空白存储」那张物品图。
   用归一化互相关（NCC），FFT 加速。NCC 对整体亮度变化不敏感，
   所以装备格被选中变亮 / 有描边时也能认出来。

2. **数字 OCR**：读余额和价格。
   游戏 UI 的数字是固定字体、固定字号，所以不做通用 OCR ——
   切出每个字符，用模板匹配认出 0-9。字模第一次用 --learn 采一遍即可，
   之后识别一次不到 1 ms，这是「高速 OCR」能跑起来的前提。

字模存放
--------
digits/ 目录下一张字符一张灰度 PNG（文件名就是字符，比如 5.png），
尺寸统一 20x28。没有字模时 ocr_ready() 返回 False，调用方应该先跑学习。
"""
import glob
import os

import numpy as np

CHAR_W, CHAR_H = 20, 32


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def to_gray(rgb):
    """(H, W, 3) uint8 -> (H, W) float32"""
    a = rgb.astype(np.float32)
    return a[:, :, 0] * 0.299 + a[:, :, 1] * 0.587 + a[:, :, 2] * 0.114


def resize_bilinear(a, w, h):
    """二维双线性缩放，只在 numpy 里做，不拉 PIL 进识别链"""
    a = np.asarray(a, dtype=np.float32)
    H, W = a.shape
    if H == 0 or W == 0:
        return np.zeros((h, w), np.float32)
    ys = np.linspace(0, H - 1, h, dtype=np.float32) if H > 1 else np.zeros(h, np.float32)
    xs = np.linspace(0, W - 1, w, dtype=np.float32) if W > 1 else np.zeros(w, np.float32)
    y0 = np.floor(ys).astype(np.int32)
    y1 = np.minimum(y0 + 1, H - 1)
    x0 = np.floor(xs).astype(np.int32)
    x1 = np.minimum(x0 + 1, W - 1)
    wy = (ys - y0)[:, None]
    wx = (xs - x0)[None, :]
    top = a[np.ix_(y0, x0)] * (1 - wx) + a[np.ix_(y0, x1)] * wx
    bot = a[np.ix_(y1, x0)] * (1 - wx) + a[np.ix_(y1, x1)] * wx
    return (top * (1 - wy) + bot * wy).astype(np.float32)


def normalize(a):
    """零均值单位方差，用于 NCC"""
    a = a.astype(np.float32)
    s = a.std()
    if s < 1e-6:
        return a - a.mean()
    return (a - a.mean()) / s


def otsu(vals, bins=64):
    """大津阈值，用来把数字从底色里分出来"""
    v = np.asarray(vals, dtype=np.float32).ravel()
    if v.size == 0:
        return 0.0
    lo, hi = float(v.min()), float(v.max())
    if hi - lo < 1e-6:
        return hi
    hist, edges = np.histogram(v, bins=bins, range=(lo, hi))
    p = hist.astype(np.float64) / max(1, hist.sum())
    centers = (edges[:-1] + edges[1:]) / 2.0
    w0 = np.cumsum(p)
    w1 = 1.0 - w0
    m0 = np.cumsum(p * centers) / np.maximum(w0, 1e-9)
    mt = float((p * centers).sum())
    m1 = (mt - np.cumsum(p * centers)) / np.maximum(w1, 1e-9)
    var = w0 * w1 * (m0 - m1) ** 2
    return float(centers[int(np.argmax(var))])


def is_flat(rgb, tol=2.0):
    """整块几乎同色 —— 独占全屏下 BitBlt 抓到黑屏时用来报警"""
    return float(rgb.astype(np.int16).std()) < tol


# ---------------------------------------------------------------------------
# 模板找图（NCC + FFT）
# ---------------------------------------------------------------------------
def _correlate(a, t):
    """
    有效互相关：out[u, v] = sum_{i,j} a[u+i, v+j] * t[i, j]

    用 FFT 算：irfft2(fft(a) * conj(fft(t))) 得到的是
    c[k] = sum_n a[n] * t[n-k]，也就是「t 的左上角落在 k」时的点积，
    正好是要的东西。a、t 都零填充到 (H+h-1, W+w-1)，所以 c 的前
    (H-h+1, W-w+1) 块是有效输出，没有循环混叠。
    """
    sh = (a.shape[0] + t.shape[0] - 1, a.shape[1] + t.shape[1] - 1)
    fa = np.fft.rfft2(a.astype(np.float32), s=sh)
    ft = np.fft.rfft2(t.astype(np.float32), s=sh)
    c = np.fft.irfft2(fa * np.conj(ft), s=sh)
    return c[:a.shape[0] - t.shape[0] + 1, :a.shape[1] - t.shape[1] + 1]


def _win_sum(ii, h, w):
    """积分图上取所有 (h, w) 窗口的和"""
    S = np.pad(ii, ((1, 0), (1, 0)))
    return S[h:, w:] - S[:-h, w:] - S[h:, :-w] + S[:-h, :-w]


def find_template(hay_rgb, tpl_rgb, threshold=0.78):
    """
    在 hay 里找 tpl，返回 (x, y, score)；找不到返回 None。
    x, y 是匹配区域左上角在 hay 里的坐标。

    分子（互相关）走 FFT，分母（窗口均值方差）走积分图。
    积分图必须用 float64：1920x1080 的 8 位图平方和累到 1e11 量级，
    float32 只剩 ~10 的相对精度，E[x²]-E[x]² 会被抹成 0，NCC 就炸了。
    """
    hay = to_gray(hay_rgb)
    tpl = to_gray(tpl_rgb)
    H, W = hay.shape
    h, w = tpl.shape
    if h > H or w > W or h < 3 or w < 3:
        return None

    mt = float(tpl.mean())
    st = float(tpl.std())
    if st < 1e-6:
        return None

    n = float(h * w)
    corr = _correlate(hay, tpl)

    hay64 = hay.astype(np.float64)
    ii = np.cumsum(np.cumsum(hay64, 0), 1)
    ii2 = np.cumsum(np.cumsum(hay64 * hay64, 0), 1)
    s1 = _win_sum(ii, h, w)
    s2 = _win_sum(ii2, h, w)
    mean = s1 / n
    var = s2 / n - mean * mean
    np.maximum(var, 0.0, out=var)
    std = np.sqrt(var)

    num = corr - n * mean * mt
    den = n * std * st

    # 窗口本身几乎纯色时 NCC 会病态爆表（分母趋零，任何一点噪声都能凑出
    # 几十分的高分）。要求窗口标准差至少是模板的四分之一，纯色区直接判无效。
    min_std = max(3.0, 0.25 * st)
    safe = (den > 1e-6) & (std >= min_std)
    score = np.full(num.shape, -1.0, dtype=np.float32)
    np.divide(num, den, out=score, where=safe)

    idx = np.unravel_index(int(np.argmax(score)), score.shape)
    best = float(score[idx])
    if best < threshold or best > 1.0001:
        return None
    return int(idx[1]), int(idx[0]), best


def find_template_multi(hay_rgb, tpl_rgb, scales=(1.0,), threshold=0.78, inset=0.0):
    """
    多尺度找图。inset=0.08 表示把模板四周各裁掉 8% 再匹配 ——
    物品格被选中时边框会变色，裁掉边框能明显提高稳定性。
    返回 (x, y, score, scale)。
    """
    th, tw = tpl_rgb.shape[:2]
    iy = int(round(th * inset))
    ix = int(round(tw * inset))
    if iy or ix:
        tpl_rgb = tpl_rgb[iy:th - iy, ix:tw - ix]
        if tpl_rgb.size == 0:
            return None

    best = None
    for s in scales:
        if abs(s - 1.0) < 1e-6:
            t = tpl_rgb
        else:
            h = max(2, int(round(tpl_rgb.shape[0] * s)))
            w = max(2, int(round(tpl_rgb.shape[1] * s)))
            t = np.stack([resize_bilinear(tpl_rgb[:, :, c], w, h) for c in range(3)],
                         axis=2)
            t = np.clip(t, 0, 255).astype(np.uint8)
        r = find_template(hay_rgb, t, threshold=threshold)
        if r and (best is None or r[2] > best[2]):
            best = (r[0], r[1], r[2], s)
    if best is None:
        return None
    # 把裁掉的那圈补回去，返回值始终对应原始模板的左上角
    return (max(0, best[0] - ix), max(0, best[1] - iy), best[2], best[3])


# ---------------------------------------------------------------------------
# 数字 OCR
# ---------------------------------------------------------------------------
class DigitOCR:
    """
    固定字体的数字识别。

    流程：边缘估计底色 -> 偏离度掩码 -> 列投影切字 -> 每个字缩放到 20x28
    -> 与字模做 NCC -> 取最高分。逗号、小数点这类矮块直接丢掉
    （对应 config 里的 no_decimal）。
    """

    def __init__(self, model_dir, min_score=0.55):
        self.model_dir = model_dir
        self.min_score = min_score
        self.model = {}       # 字符 -> [模板数组, ...]，同一个字符可以有多张
        self.reload()

    @staticmethod
    def _label_of(stem):
        """文件名 0.png / 0_2.png / K_3.png 都取第一个字符做标签"""
        return stem.split("_")[0]

    def reload(self):
        self.model = {}
        for p in sorted(glob.glob(os.path.join(self.model_dir, "*.png"))):
            stem = os.path.splitext(os.path.basename(p))[0]
            ch = self._label_of(stem)
            if len(ch) != 1:
                continue
            try:
                from PIL import Image
                a = np.asarray(Image.open(p).convert("L"), dtype=np.float32)
            except Exception:
                continue
            if a.size == 0:
                continue
            a = resize_bilinear(a, CHAR_W, CHAR_H)
            self.model.setdefault(ch, []).append(normalize(a))
        return len(self.model)

    def ocr_ready(self):
        return all(str(d) in self.model for d in range(10))

    # --- 切分 ---
    def segment(self, rgb, min_height_ratio=0.55):
        """
        返回 [(x0, x1, y0, y1, gray_crop), ...]，按从左到右排列。

        过滤阈值不按整幅图高算，而是按「本次切出来的段里最高的那批」算 ——
        截图上下带了空白、或者数字被抗锯齿削薄时，按图高算会把整行数字全滤掉。
        """
        g = to_gray(rgb)
        H, W = g.shape
        if H < 3 or W < 3:
            return []
        border = np.concatenate([g[0], g[-1], g[:, 0], g[:, -1]])
        bg = float(np.median(border))
        diff = np.abs(g - bg)
        thr = max(otsu(diff), 16.0)
        mask = diff > thr
        if not mask.any():
            return []

        colsum = mask.sum(axis=0)
        raw = []
        i = 0
        while i < W:
            if colsum[i] == 0:
                i += 1
                continue
            j = i
            while j < W and colsum[j] > 0:
                j += 1
            sub = mask[:, i:j]
            rows = np.where(sub.any(axis=1))[0]
            if rows.size:
                raw.append((i, j, int(rows[0]), int(rows[-1]) + 1))
            i = j
        if not raw:
            return []

        heights = sorted(y1 - y0 for (_, _, y0, y1) in raw)
        ref_h = heights[-1] if len(heights) <= 3 else float(np.percentile(heights, 80))
        min_h = max(4, int(ref_h * min_height_ratio))
        min_w = max(3, int(ref_h * 0.30))

        segs = []
        for (x0, x1, y0, y1) in raw:
            if (y1 - y0) < min_h:
                continue                      # 太矮：噪点
            if (x1 - x0) < min_w:
                continue                      # 太细：边缘碎片
            segs.append((x0, x1, y0, y1, g[y0:y1, x0:x1]))
        return segs

    # --- 识别 ---
    def read(self, rgb, debug=None):
        """
        返回 dict:
            value   int 或 None   识别出的数字；只有全部认对且都是纯数字时才有值
            text    识别结果，认不出的字符用 '?' 占位
            ok      True 表示整行都认出来了、可以放心用
            conf    平均分
            n       text 的长度

        认不出的字符写成 '?'，只要出现一个 '?' 就判 ok=False，调用方据此重试。
        """
        segs = self.segment(rgb)
        if not segs:
            return {"value": None, "text": "", "ok": False, "conf": 0.0, "n": 0}

        chars = []
        scores = []
        for (_, _, _, _, crop) in segs:
            c = normalize(resize_bilinear(crop, CHAR_W, CHAR_H))
            best_ch, best_sc = None, -2.0
            for ch, mdls in self.model.items():
                sc = max(float((c * m).mean()) for m in mdls)
                if sc > best_sc:
                    best_ch, best_sc = ch, sc
            if best_sc < self.min_score or best_ch is None:
                chars.append("?")
                scores.append(max(0.0, best_sc))
            else:
                chars.append(best_ch)
                scores.append(best_sc)

        text = "".join(chars)
        # 余额和价格左边常有个货币图标，它会被认成一个 '?'。那个位置本来
        # 就不可能是数字，所以只把最前面那一个去掉；中间的、末尾的 '?'
        # 仍然算失败，一点不含糊。
        if text.startswith("?"):
            text = text[1:]
        ok = "?" not in text and bool(text)
        value = int(text) if (ok and text.isdigit()) else None
        return {
            "value": value,
            "text": text,
            "ok": value is not None,
            "conf": float(np.mean(scores)) if scores else 0.0,
            "n": len(text),
        }

    # --- 学习 ---
    def learn(self, rgb, text, save_debug=None):
        """
        从一块截图学字模。text 是这块区域里真实的数字，比如 "250000" 或 "3,160K"。
        切出来的字符数必须和 text 里的字符数一致，否则调用方该换一块更干净的图。
        """
        segs = self.segment(rgb)
        want = [c for c in str(text).upper() if c.isdigit() or c == "K"]
        if len(segs) != len(want):
            raise ValueError("切出 %d 个字符，但你输入了 %d 个（%s）"
                             % (len(segs), len(want), text))
        from PIL import Image
        os.makedirs(self.model_dir, exist_ok=True)
        saved = []
        for (_, _, _, _, crop), ch in zip(segs, want):
            # 同名文件自动递增编号，同一个字可以攒多张模板
            n = 1
            while True:
                p = os.path.join(self.model_dir,
                                 "%s.png" % ch if n == 1 else "%s_%d.png" % (ch, n))
                if not os.path.exists(p):
                    break
                n += 1
            a = resize_bilinear(crop, CHAR_W, CHAR_H)
            a = np.clip(a, 0, 255).astype(np.uint8)
            Image.fromarray(a, mode="L").save(p)
            saved.append(p)
        if save_debug:
            Image.fromarray(rgb).save(save_debug)
        self.reload()
        return saved


# ---------------------------------------------------------------------------
# 调试
# ---------------------------------------------------------------------------
def save_rgb(path, rgb):
    try:
        from PIL import Image
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        Image.fromarray(np.ascontiguousarray(rgb)).save(path)
        return path
    except Exception:
        return None
