/* 前端只认 /api/* 这几个接口：一个 state 快照 + 一条 SSE 事件流。
   界面上的所有数字都来自后端，这里不做任何业务判断。 */

const MAX_LOG = 600;

const $ = (id) => document.getElementById(id);
const els = {
  screen: $("meta-screen"),
  status: $("status"), statusText: $("status-text"), statusExtra: $("status-extra"),
  helmetDone: $("helmet-done"), armorDone: $("armor-done"),
  helmetGoal: $("helmet-goal"), armorGoal: $("armor-goal"),
  goalHelmet: $("goal-helmet"), goalArmor: $("goal-armor"),
  balSub: $("bal-sub"), ovTag: $("ov-tag"),
  priceMin: $("price-min"), priceMax: $("price-max"),
  ovLabel: $("ov-label"), livePrice: $("live-price"),
  tabLog: $("tab-log"), tabBuy: $("tab-buy"), buylog: $("buylog"),
  btnStart: $("btn-start"), btnStartText: $("btn-start-text"),
  btnProbe: $("btn-probe"), btnCalib: $("btn-calib"), btnPin: $("btn-pin"),
  cfgFile: $("cfg-file"), cfgScreen: $("cfg-screen"),
  cfgCount: $("cfg-count"), cfgTime: $("cfg-time"), cfgEmpty: $("cfg-empty"),
  log: $("log"), logEmpty: $("log-empty"),
  conn: $("conn"), btnClear: $("btn-clear"), toast: $("toast"),
};

let state = { mode: "idle", running: false };
let pinned = false;
// 已抢数量和最近成交价，全由后端推过来
let stats = { helmet: 0, armor: 0, last_price: null };
// 高速刷价时后端吐 PRICE 行，走单独通道进来；跟「成交价」不是一回事
let live = { value: null, ok: false };

/* ---------------------------------------------------------------- 工具 */
function fmtNum(v) {
  return String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

let toastTimer = 0;
function toast(msg, tone) {
  els.toast.textContent = msg;
  els.toast.dataset.tone = tone || "";
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, 2600);
}

/* 目标数量存在本地，关掉再开还在 */
const GOAL_KEY = "glm.goals";
const PRICE_KEY = "glm.price";

function clampGoal(v, dflt) {
  const n = parseInt(v, 10);
  if (!Number.isFinite(n) || n < 0) return dflt;
  return Math.min(99, n);
}

/* 价格区间界面上按「万」填，传给后端时乘 10000 */
function readPrice() {
  const lo = clampGoal(els.priceMin && els.priceMin.value, 20);
  const hi = clampGoal(els.priceMax && els.priceMax.value, 30);
  const a = Math.max(1, lo);
  const b = Math.max(1, hi);
  return { min: Math.min(a, b), max: Math.max(a, b) };
}

function readGoals() {
  try {
    const g = JSON.parse(localStorage.getItem(GOAL_KEY) || "{}");
    return { helmet: clampGoal(g.helmet, 1), armor: clampGoal(g.armor, 1) };
  } catch (e) {
    return { helmet: 1, armor: 1 };
  }
}

function writeGoals(g) {
  try { localStorage.setItem(GOAL_KEY, JSON.stringify(g)); } catch (e) { /* 隐身模式下存不了，无所谓 */ }
}

/* ---------------------------------------------------------------- 渲染 */
function syncMode() {
  // 窗口小了就切精简形态：运行中会自动缩小并置顶，手动置顶也一样
  const work = pinned || state.mode === "running";
  document.body.dataset.mode = work ? "work" : "idle";
  renderStats();
}

function renderStats() {
  const g = readGoals();
  const done = { helmet: stats.helmet || 0, armor: stats.armor || 0 };

  els.helmetDone.textContent = done.helmet;
  els.armorDone.textContent = done.armor;
  els.goalHelmet.dataset.full = done.helmet >= g.helmet ? "on" : "";
  els.goalArmor.dataset.full = done.armor >= g.armor ? "on" : "";

  const all = done.helmet >= g.helmet && done.armor >= g.armor;
  els.ovTag.hidden = !all;
  if (all) els.ovTag.textContent = "已抢够";

  // 小窗里盯的是刷到多少钱，大窗里看的是抢到几件，所以两边标题不一样
  const work = document.body.dataset.mode === "work";
  els.ovLabel.textContent = work ? "当前最低价" : "抢购概况";
  const hasLive = live.value !== null && live.value !== undefined;
  els.livePrice.textContent = hasLive ? fmtNum(live.value) : "--";
  els.livePrice.dataset.empty = hasLive ? "" : "on";
  const hasPrice = stats.last_price !== null && stats.last_price !== undefined;
  els.balSub.textContent = work
    ? "刷价中，落到 20w ~ 30w 就自动买"
    : (hasPrice ? "成交价 " + fmtNum(stats.last_price) : "成交价 --");
}

/* ---------------------------------------------------- 运行日志 / 抢购日记 */
let tab = "log";

function switchTab(name) {
  tab = name;
  const isLog = name === "log";
  els.tabLog.setAttribute("aria-selected", isLog ? "true" : "false");
  els.tabBuy.setAttribute("aria-selected", isLog ? "false" : "true");
  els.log.hidden = !isLog;
  els.buylog.hidden = isLog;
  els.logEmpty.hidden = !isLog || els.log.children.length > 0;
}

function buyRow(p) {
  const li = document.createElement("li");
  const t = document.createElement("span"); t.className = "bt"; t.textContent = p.t || "";
  const k = document.createElement("span"); k.className = "bk"; k.textContent = p.kind || "";
  const v = document.createElement("span"); v.className = "bp"; v.textContent = fmtNum(p.price);
  const n = document.createElement("span"); n.className = "bn"; n.textContent = "第 " + (p.no || 1) + " 件";
  li.append(t, k, v, n);
  return li;
}

function renderPurchases(list) {
  els.buylog.innerHTML = "";
  if (!list || !list.length) {
    const li = document.createElement("li");
    li.className = "bempty";
    li.textContent = "还没抢到东西。成交一件就记一行，最新的在最上面。";
    els.buylog.append(li);
    return;
  }
  list.slice().reverse().forEach((p) => els.buylog.append(buyRow(p)));
}

function pushPurchase(p) {
  const first = els.buylog.querySelector(".bempty");
  if (first) first.remove();
  els.buylog.insertBefore(buyRow(p), els.buylog.firstChild);
}

/* 输入框只在启动时灌一次值，之后不回写，免得把用户正在敲的数字冲掉 */
function initGoalInputs() {
  const g = readGoals();
  els.helmetGoal.value = g.helmet;
  els.armorGoal.value = g.armor;

  if (els.priceMin && els.priceMax) {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(PRICE_KEY) || "null"); } catch (e) {}
    if (saved && saved.min) els.priceMin.value = saved.min;
    if (saved && saved.max) els.priceMax.value = saved.max;
    const commitPrice = () => {
      const p = readPrice();
      els.priceMin.value = p.min;
      els.priceMax.value = p.max;
      localStorage.setItem(PRICE_KEY, JSON.stringify(p));
    };
    [els.priceMin, els.priceMax].forEach((el) => {
      el.addEventListener("change", commitPrice);
      el.addEventListener("blur", commitPrice);
    });
  }
}

function bindGoalInput(input, key) {
  const commit = () => {
    const g = readGoals();
    g[key] = clampGoal(input.value, g[key]);
    input.value = g[key];
    writeGoals(g);
    renderStats();
  };
  input.addEventListener("change", commit);
  input.addEventListener("blur", commit);
}

function applyPin(on) {
  pinned = !!on;
  els.btnPin.textContent = pinned ? "取消置顶" : "置顶";
  els.btnPin.dataset.active = pinned ? "on" : "";
  syncMode();
}

function setStatus(mode, extra) {
  const map = { idle: "待机", running: "运行中", calibrating: "标定中" };
  els.status.dataset.mode = mode;
  els.statusText.textContent = map[mode] || mode;
  els.statusExtra.textContent = extra || "";
  // 运行中切到精简形态：藏掉配置详情和说明，把日志让出来
  syncMode();

  const busy = mode !== "idle";
  els.btnStart.disabled = mode === "calibrating";
  els.btnProbe.disabled = busy;
  els.btnCalib.disabled = busy;

  if (mode === "running") {
    els.btnStart.dataset.tone = "stop";
    els.btnStartText.textContent = "停止";
  } else {
    els.btnStart.dataset.tone = "";
    els.btnStartText.textContent = mode === "calibrating" ? "标定中…" : "开始运行";
  }
}

function renderConfig(cfg) {
  const scr = cfg.screen || {};
  const wh = (scr.w && scr.h) ? scr.w + "×" + scr.h : "—";
  const dpi = scr.dpi ? " @" + scr.dpi + "dpi" : "";
  const file = cfg.path ? cfg.path.split(/[\\\\/]/).pop() : "";

  els.cfgFile.textContent = file || "—";
  els.cfgScreen.textContent = file ? wh + dpi : "—";
  els.cfgCount.textContent = cfg.count
    ? cfg.count + " 个" + (cfg.partial ? " · 部分未重标" : "")
    : "—";
  // 标定时间只留「月-日 时:分」，年份对判断新旧没意义
  els.cfgTime.textContent = cfg.calibrated
    ? String(cfg.calibrated).slice(5, 16)
    : "—";
  els.cfgEmpty.hidden = !!file;

  // 顶栏那行是给「配置对不对」一眼确认用的，跟左下角摘要同源
  els.screen.textContent = file
    ? file + " · " + wh + " · " + (cfg.count ?? 0) + " 个区域"
    : "读不到 config.json";
}
function appendLog(rec) {
  // scrollTop 还是 0 说明用户从没往上翻过，应该一直跟着最新一条走
  const nearBottom =
    els.log.scrollTop <= 0 ||
    els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 48;

  const li = document.createElement("li");
  li.dataset.level = rec.level || "INFO";

  const t = document.createElement("span");
  t.className = "t";
  t.textContent = rec.t || "";

  const lv = document.createElement("span");
  lv.className = "lv";
  lv.textContent = rec.level || "INFO";

  const msg = document.createElement("span");
  msg.className = "msg";
  msg.textContent = rec.msg || "";

  li.append(t, lv, msg);
  els.log.append(li);
  els.logEmpty.hidden = true;

  while (els.log.children.length > MAX_LOG) els.log.firstChild.remove();
  if (nearBottom) els.log.scrollTop = els.log.scrollHeight;
}

/* ---------------------------------------------------------------- 数据 */
async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opt);
  return res.json();
}

async function loadState() {
  try {
    const s = await api("/api/state");
    state = s;
    setStatus(s.mode, s.round ? `第 ${s.round} 轮` : "");
    applyPin(s.pinned);
    renderConfig(s.config || {});
    if (s.stats) stats = s.stats;
    renderStats();
    els.log.innerHTML = "";
    (s.logs || []).forEach(appendLog);
    if (s.price) live = s.price;
    renderPurchases(s.purchases);
    // 两个面板的显隐只由 switchTab 一处决定，别在这里再动 logEmpty
    switchTab(tab);
  } catch (e) {
    els.screen.textContent = "连不上后端";
  }
}

function connect() {
  const es = new EventSource("/api/events");

  es.onopen = () => { els.conn.dataset.state = "on"; els.conn.textContent = "已连接"; };
  es.onerror = () => { els.conn.dataset.state = "off"; els.conn.textContent = "断开，重连中"; };

  es.addEventListener("log", (ev) => appendLog(JSON.parse(ev.data)));
  es.addEventListener("stats", (ev) => { stats = JSON.parse(ev.data); renderStats(); });
  es.addEventListener("price", (ev) => {
    live = JSON.parse(ev.data);
    renderStats();
  });
  es.addEventListener("purchase", (ev) => pushPurchase(JSON.parse(ev.data)));
  es.addEventListener("pin", (ev) => applyPin(JSON.parse(ev.data).on));
  es.addEventListener("config", (ev) => {
    const c = JSON.parse(ev.data);
    state.config = c;
    renderConfig(c);
  });
  es.addEventListener("state", (ev) => {
    const s = JSON.parse(ev.data);
    state.mode = s.mode;
    state.running = s.mode === "running";
    setStatus(s.mode, s.round ? `第 ${s.round} 轮` : "");
  });
}

/* ---------------------------------------------------------------- 交互 */
els.btnStart.addEventListener("click", async () => {
  if (state.mode === "running") {
    const r = await api("/api/stop", {});
    if (!r.ok) toast(r.err || "停止失败", "bad");
    return;
  }
  if (state.mode !== "idle") return;
  const g = readGoals();
  const p = readPrice();
  const r = await api("/api/start", {
    args: [
      "--live",
      "--helmet", String(g.helmet),
      "--armor", String(g.armor),
      "--price-min", String(p.min * 10000),
      "--price-max", String(p.max * 10000),
    ],
  });
  if (!r.ok) toast(r.err || "启动失败", "bad");
});

els.btnProbe.addEventListener("click", async () => {
  const r = await api("/api/probe", {});
  if (!r.ok) toast(r.err || "启动失败", "bad");
});

els.btnCalib.addEventListener("click", async () => {
  const r = await api("/api/calibrate", {});
  if (!r.ok) toast(r.err || "标定打不开", "bad");
});

els.btnPin.addEventListener("click", async () => {
  const r = await api("/api/pin", { on: !pinned });
  if (!r.ok) toast(r.err || "置顶失败", "bad");
  else applyPin(r.pinned);
});

els.tabLog.addEventListener("click", () => switchTab("log"));
els.tabBuy.addEventListener("click", () => switchTab("buy"));

els.btnClear.addEventListener("click", () => {
  els.log.innerHTML = "";
  els.logEmpty.hidden = false;
});

// 窗口重新获得焦点时补一次快照，防止 SSE 断线期间漏事件
window.addEventListener("focus", loadState);

initGoalInputs();
bindGoalInput(els.helmetGoal, "helmet");
bindGoalInput(els.armorGoal, "armor");
// ?tab=buy 可以直接落到抢购日记，方便截图和定位
const wantTab = new URLSearchParams(location.search).get("tab");
switchTab(wantTab === "buy" ? "buy" : "log");
renderStats();
loadState().then(connect);
