const $ = (id) => document.getElementById(id);
// ---- 壳层 i18n / 时区桥接 -------------------------------------------------
// 语言包与内核见 i18n/index.js（window.G4W_I18N_API）。这里只负责壳层取词与时间格式化；
// 内核已对缺失键、非法时区、Intl 不可用做了回落，下面的裸兜底只在内核整体缺失时生效。
const I18N = window.G4W_I18N_API || null;
const uiPrefs = { locale: "zh-CN", timezone: "" };
let uiPrefsHydrated = false;

function t(key, ...args) {
  return I18N ? I18N.t(key, ...args) : key;
}
function fmtTime(ms) {
  if (I18N) return I18N.fmtTime(ms);
  return new Date(ms).toLocaleTimeString([], { hour12: false });
}
function fmtHM(ms) {
  if (I18N) return I18N.fmtHM(ms);
  return new Date(ms).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit" });
}
function fmtDate(ms) {
  if (I18N) return I18N.fmtDate(ms);
  const d = new Date(ms);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}
function fmtWeekday(ms) {
  if (I18N) return I18N.fmtWeekday(ms);
  return new Date(ms).toLocaleDateString([], { weekday: "short" });
}
function fmtShortDate(value) {
  if (I18N) return I18N.fmtShortDate(value);
  const d = new Date(value + "T12:00:00");
  return (d.getMonth() + 1) + "/" + d.getDate();
}
function fmtHourLabel(hour) {
  if (I18N) return I18N.fmtHourLabel(hour);
  return String(hour).padStart(2, "0") + ":00";
}
let dashboardData = null;
let promptData = null;
let settingsDirty = false;
let timelineData = null;
let lastTimelineAttempt = 0;
let lastDiaryAttempt = 0;
let timelineMode = "day";
let timelineDate = "";
let timelineSlotHeight = 30;
let timelineMonthZoom = 1;
let timelineCategoryFilter = "";

// ---- dashboard 登录鉴权(ga-admin 风格:密码登录 + 会话 cookie) ----
// 前端 JS 错误上报（诊断用）：写 G4W-data/debug-js.log，便于远程定位
window.addEventListener("error", (e) => {
  try {
    fetch("/api/debug-log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ msg: String((e && e.message) || e), src: String((e && e.filename) || ""), line: e && e.lineno }) });
  } catch (_) {}
});
window.addEventListener("unhandledrejection", (e) => {
  try {
    fetch("/api/debug-log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ msg: "unhandledrejection: " + String((e && e.reason && e.reason.message) || (e && e.reason) || e) }) });
  } catch (_) {}
});

function showLogin() {
  const overlay = $("login-overlay");
  if (overlay) overlay.hidden = false;
}

function hideLogin() {
  const overlay = $("login-overlay");
  if (overlay) overlay.hidden = true;
}

function setAuthMode(initialized) {
  const setupMode = $("login-mode-setup");
  const loginMode = $("login-mode-login");
  if (setupMode) setupMode.hidden = initialized;
  if (loginMode) loginMode.hidden = !initialized;
}

async function ensureAuth() {
  try {
    const res = await fetch("/api/auth/status", { cache: "no-store" });
    if (!res.ok) throw new Error(`status ${res.status}`);
    const state = await res.json();
    setAuthMode(!!state.initialized);
    if (state.authenticated) { hideLogin(); return true; }
    showLogin();
    return false;
  } catch { showLogin(); return false; }
}

async function submitSetup() {
  const username = $("setup-username").value.trim();
  const password = $("setup-password").value;
  const password2 = $("setup-password2").value;
  const errorEl = $("login-error");
  if (!username) { errorEl.textContent = t("auth.needUser"); errorEl.hidden = false; return; }
  if (password.length < 8) { errorEl.textContent = t("auth.passwordMin8"); errorEl.hidden = false; return; }
  if (password !== password2) { errorEl.textContent = t("auth.passwordMismatch"); errorEl.hidden = false; return; }
  errorEl.hidden = true;
  try {
    const res = await fetch("/api/auth/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      $("setup-password").value = "";
      $("setup-password2").value = "";
      hideLogin();
      refresh();
      connectEvents();
      return;
    }
    errorEl.textContent = data.error || t("auth.setupFailed");
    errorEl.hidden = false;
  } catch {
    errorEl.textContent = t("auth.serverUnreachable");
    errorEl.hidden = false;
  }
}

async function submitLogin() {
  const username = $("login-username").value.trim();
  const password = $("login-password").value;
  const errorEl = $("login-error");
  if (!username || !password) {
    errorEl.textContent = t("auth.needCredentials");
    errorEl.hidden = false;
    return;
  }
  errorEl.hidden = true;
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      $("login-password").value = "";
      hideLogin();
      refresh();
      connectEvents();
      return;
    }
    errorEl.textContent = data.error || t("auth.badCredentials");
    errorEl.hidden = false;
  } catch {
    errorEl.textContent = t("auth.serverUnreachable");
    errorEl.hidden = false;
  }
}

async function submitPasswordChange() {
  const oldPwd = $("setting-old-password").value;
  const newPwd = $("setting-new-password").value;
  const newPwd2 = $("setting-new-password2").value;
  const messageEl = $("password-message");
  if (newPwd.length < 8) { messageEl.textContent = t("auth.newPasswordMin8"); messageEl.style.color = "#ff6b6b"; return; }
  if (newPwd !== newPwd2) { messageEl.textContent = t("auth.newPasswordMismatch"); messageEl.style.color = "#ff6b6b"; return; }
  try {
    const res = await fetch("/api/auth/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("login-username").value.trim() || "admin", oldPassword: oldPwd, newPassword: newPwd }),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      $("setting-old-password").value = "";
      $("setting-new-password").value = "";
      $("setting-new-password2").value = "";
      messageEl.textContent = t("auth.passwordUpdated");
      messageEl.style.color = "";
    } else {
      messageEl.textContent = data.error || t("auth.changeFailed");
      messageEl.style.color = "#ff6b6b";
    }
  } catch {
    messageEl.textContent = t("auth.serverUnreachable");
    messageEl.style.color = "#ff6b6b";
  }
}

async function logoutAll() {
  if (!window.confirm(t("auth.logoutAllConfirm"))) return;
  try {
    await fetchJson("/api/auth/revoke-all", { method: "POST" });
    disconnectEvents();
    setAuthMode(true);
    showLogin();
  } catch (error) {
    showToast(error.message || t("auth.serverUnreachable"), true);
  }
}

// 全局拦截:任何 API 返回 401 → 回到登录页(会话过期等)
const _g4wOrigFetch = window.fetch;
window.fetch = async (...args) => {
  const res = await _g4wOrigFetch(...args);
  const url = String(args[0] || "");
  if (res.status === 401 && !url.includes("/api/auth/")) showLogin();
  return res;
};

function filterTimelineEvents(events) {
  return timelineCategoryFilter ? events.filter((event) => (event.categoryId || "life") === timelineCategoryFilter) : events;
}

function timelineMix(event) {
  // 时长 → 分类色混合比例:最短 10%,12 小时封顶 45%,线性
  return Math.round(10 + Math.min(1, eventMinutes(event) / 720) * 35);
}

function donutPolar(cx, cy, radius, angleDeg) {
  const rad = angleDeg * Math.PI / 180;
  return { x: cx + radius * Math.cos(rad), y: cy + radius * Math.sin(rad) };
}

function donutSegmentPath(cx, cy, outer, inner, start, end) {
  const outerStart = donutPolar(cx, cy, outer, start);
  const outerEnd = donutPolar(cx, cy, outer, end);
  const innerEnd = donutPolar(cx, cy, inner, end);
  const innerStart = donutPolar(cx, cy, inner, start);
  const span = end - start;
  const large = Math.abs(span) > 180 ? 1 : 0;
  const sweep = span > 0 ? 1 : 0; // 递减角度=逆时针(参考站方向)
  return `M ${outerStart.x.toFixed(2)} ${outerStart.y.toFixed(2)} A ${outer} ${outer} 0 ${large} ${sweep} ${outerEnd.x.toFixed(2)} ${outerEnd.y.toFixed(2)} L ${innerEnd.x.toFixed(2)} ${innerEnd.y.toFixed(2)} A ${inner} ${inner} 0 ${large} ${sweep ? 0 : 1} ${innerStart.x.toFixed(2)} ${innerStart.y.toFixed(2)} Z`;
}

function renderDonut(categories, totalMinutes) {
  const cx = 140, cy = 140, inner = 46, gapDeg = 2.5;
  const total = totalMinutes || 1;
  if (!categories.length) return "";
  // 收集扇区元数据(起点 0°=3点钟方向,逆时针累积,与参考站一致)
  let angle = 0;
  const meta = [];
  for (const [category, value] of categories) {
    const sweep = value / total * 360;
    const start = angle - gapDeg / 2;
    const end = angle - sweep + gapDeg / 2;
    const mid = (start + end) / 2;
    const rad = mid * Math.PI / 180;
    const pct = (value / total * 100).toFixed(1).replace(/\.0$/, "");
    const text = `${categoryLabel(category)} ${pct}%`;
    const anchor = Math.cos(rad) >= 0 ? "start" : "end";
    let w = 34;
    try {
      const mctx = document.createElement("canvas").getContext("2d");
      mctx.font = "8.5px Inter, sans-serif";
      w = mctx.measureText(text).width + 6;
    } catch (e) { /* fallback */ }
    meta.push({ category, sweep, start, end, rad, text, anchor, w });
    angle -= sweep;
  }
  const boxAt = (it, outer, dx, dy) => {
    const x = cx + (outer + 8) * Math.cos(it.rad) + dx;
    const y = cy + (outer + 8) * Math.sin(it.rad) + dy;
    // 行高≈字号(与 Recharts 一致,dominant-baseline 中央对齐):标签可贴弧线分布
    return it.anchor === "start"
      ? { x0: x, x1: x + it.w, y0: y - 5.85, y1: y + 5.85 }
      : { x0: x - it.w, x1: x, y0: y - 5.85, y1: y + 5.85 };
  };
  const overlaps = (a, b) => a.x0 < b.x1 - 0.5 && b.x0 < a.x1 - 0.5 && a.y0 < b.y1 - 0.5 && b.y0 < a.y1 - 0.5;
  const hitsRing = (p, outer) => {
    const nx = Math.max(p.x0, Math.min(cx, p.x1)), ny = Math.max(p.y0, Math.min(cy, p.y1));
    return Math.hypot(nx - cx, ny - cy) < outer - 1;
  };
  const outOfBox = (p) => p.x0 < 2 || p.x1 > 338 || p.y0 < 2 || p.y1 > 278;
  // 方案A:纯径向(参考站式)——标签固定在自己扇区方向、环外 8 单位,无微移
  const pureRadial = (outer) => {
    const lbs = meta.map((it) => ({ it, p: boxAt(it, outer, 0, 0) }));
    for (let i = 0; i < lbs.length; i++)
      for (let j = i + 1; j < lbs.length; j++)
        if (overlaps(lbs[i].p, lbs[j].p)) return null;
    for (const l of lbs) if (hitsRing(l.p, outer) || outOfBox(l.p)) return null;
    return lbs.map((l) => ({ ...l.it, dx: 0, dy: 0 }));
  };
  // 方案A2:径向+最小修正(纯径向仅差几个像素时,按角度顺序做 1px 级纵向让位,保持围绕环形态)
  const pureFix = (outer) => {
    const items = meta.map((m) => ({ ...m, dx: 0, dy: 0 }));
    for (let iter = 0; iter < 24; iter++) {
      let moved = false;
      for (let i = 0; i < items.length; i++)
        for (let j = i + 1; j < items.length; j++) {
          const a = boxAt(items[i], outer, items[i].dx, items[i].dy);
          const b = boxAt(items[j], outer, items[j].dx, items[j].dy);
          if (a.x0 < b.x1 - 0.5 && b.x0 < a.x1 - 0.5 && a.y0 < b.y1 - 0.5 && b.y0 < a.y1 - 0.5) {
            // 角度小的在上方:向上让位,保持扇区顺序(引线不交叉)
            if (Math.sin(items[i].rad) < Math.sin(items[j].rad)) { items[i].dy -= 1; items[j].dy += 1; }
            else { items[i].dy += 1; items[j].dy -= 1; }
            moved = true;
          }
        }
      if (!moved) break;
    }
    const lbs = items.map((it) => ({ it, p: boxAt(it, outer, it.dx, it.dy) }));
    for (let i = 0; i < lbs.length; i++)
      for (let j = i + 1; j < lbs.length; j++)
        if (overlaps(lbs[i].p, lbs[j].p)) return null;
    for (const l of lbs) if (hitsRing(l.p, outer) || outOfBox(l.p)) return null;
    return items;
  };
  const freeMove = (outer) => {
    const MIN_R = outer + 9;
    const items = meta.map((m) => ({ ...m, dx: 0, dy: 0 }));
    for (let iter = 0; iter < 120; iter++) {
      let moved = false;
      for (let i = 0; i < items.length; i++) {
        for (let j = i + 1; j < items.length; j++) {
          const a = boxAt(items[i], outer, items[i].dx, items[i].dy);
          const b = boxAt(items[j], outer, items[j].dx, items[j].dy);
          if (a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1) {
            const ox = Math.min(a.x1 - b.x0, b.x1 - a.x0);
            const oy = Math.min(a.y1 - b.y0, b.y1 - a.y0);
            if (ox <= oy) { items[i].dx -= ox / 2; items[j].dx += ox / 2; }
            // 纵向让位按角度顺序(角度小的在上方),避免推乱扇区顺序导致引线交叉
            else if (Math.sin(items[i].rad) < Math.sin(items[j].rad)) { items[i].dy -= oy / 2; items[j].dy += oy / 2; }
            else { items[i].dy += oy / 2; items[j].dy -= oy / 2; }
            moved = true;
          }
        }
      }
      for (const it of items) {
        let b = boxAt(it, outer, it.dx, it.dy);
        const nx = Math.max(b.x0, Math.min(cx, b.x1)), ny = Math.max(b.y0, Math.min(cy, b.y1));
        const d = Math.hypot(nx - cx, ny - cy);
        if (d < MIN_R) {
          const ang = Math.atan2((b.y0 + b.y1) / 2 - cy, (b.x0 + b.x1) / 2 - cx);
          const push = MIN_R - d + 1;
          it.dx += Math.cos(ang) * push;
          it.dy += Math.sin(ang) * push;
          moved = true;
        }
        b = boxAt(it, outer, it.dx, it.dy);
        if (b.x0 < 4) it.dx += 4 - b.x0;
        if (b.x1 > 336) it.dx -= b.x1 - 336;
        b = boxAt(it, outer, it.dx, it.dy);
        if (b.y0 < 4) it.dy += 4 - b.y0;
        if (b.y1 > 276) it.dy -= b.y1 - 276;
      }
      if (!moved) break;
    }
    const lbs = items.map((it) => ({ it, p: boxAt(it, outer, it.dx, it.dy) }));
    for (let i = 0; i < lbs.length; i++)
      for (let j = i + 1; j < lbs.length; j++)
        if (overlaps(lbs[i].p, lbs[j].p)) return null;
    for (const l of lbs) if (hitsRing(l.p, outer)) return null;
    return items;
  };
  let outer = 94, items = null;
  // 方案A优先:纯径向(参考站式)全范围扫描,任一 outer 可行即采用(标签贴环、引线短)
  for (; outer >= 48; outer -= 2) {
    items = pureRadial(outer);
    if (items) break;
  }
  let mode = items ? "pure" : null;
  if (!items) {
    // 方案A2:径向+最小修正(保持围绕环形态,仅做 1px 级纵向让位)
    for (outer = 94; outer >= 48; outer -= 2) {
      items = pureFix(outer);
      if (items) break;
    }
    mode = items ? "fix" : null;
  }
  if (!items) {
    // 方案B兜底:自由微移(保证零重叠零压环,引线可能较长)
    for (outer = 94; outer >= 48; outer -= 2) {
      items = freeMove(outer);
      if (items) break;
    }
    mode = items ? "free" : null;
  }
  if (!items) {
    outer = 94;
    items = meta.map((m) => ({ ...m, dx: 0, dy: 0 }));
    mode = "raw";
  }
  const parts = meta.map((m) => m.sweep > gapDeg
    ? `<path class="donut-segment" d="${donutSegmentPath(cx, cy, outer, inner, m.start, m.end)}" style="--seg-color:${timelineColor(m.category)}" data-timeline-filter="${esc(m.category)}" />`
    : "").join("");
  const labels = items.map((it) => {
    const x = cx + (outer + 8) * Math.cos(it.rad) + it.dx;
    const y = cy + (outer + 8) * Math.sin(it.rad) + it.dy;
    // pure/fix 模式(标签贴环、无重叠)不画引线,与项目时间轴一致;free 模式才用引线标明归属
    const lead = mode === "pure" || mode === "fix"
      ? ""
      : `<line class="donut-leader" x1="${(cx + (outer + 8) * Math.cos(it.rad)).toFixed(1)}" y1="${(cy + (outer + 8) * Math.sin(it.rad)).toFixed(1)}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" />`;
    return `${lead}<text class="donut-label" x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="${it.anchor}">${esc(it.text)}</text>`;
  }).join("");
  return `<svg class="donut-chart" viewBox="0 0 340 280">${parts}${labels}</svg>`;
}

let diaryData = null;
let currentDiaryDate = "";
let diaryMonth = "";
let sopData = null;
let currentSopPath = "";
let sopScope = localStorage.getItem("g4w-sop-scope") || "";
let lastSopAttempt = 0;
/** 展开中的目录路径集合（相对 scope 根，"" = 根） */
let sopExpanded = new Set();
let modelsLoading = false;
let eventFilter = "all";
let knowledgeQuery = "";
let knowledgeTag = "";

const fallback = {
  updatedAt: Date.now() / 1000,
  system: { name: "G4W", user: t("kpi.localUser"), status: "standby", model: "--", vector: false },
  metrics: { workers: 0, runningWorkers: 0, memoryProfiles: 0, memoryBackups: 0, knowledgeDocuments: 0, knowledgeChunks: 0, pendingEvents: 0 },
  workers: [], events: [], knowledge: { documents: 0, chunks: 0, vector: false, root: "--", items: [] },
  memory: { profiles: 0, backups: 0, root: "--", active: false, items: [] },
  configuration: {}
};

function esc(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function statusLabel(value) {
  return ({ running: "RUNNING", starting: "STARTING", completed: "COMPLETED", failed: "FAILED", archived: "ARCHIVED", sleeping: "SLEEPING", standby: "STANDBY", online: "ONLINE" })[value] || String(value || "UNKNOWN").toUpperCase();
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function jsonText(value, maxItems = 80) {
  let output = value;
  if (Array.isArray(value) && value.length > maxItems) output = [...value.slice(0, maxItems), { note: `${t("json.truncated", maxItems, value.length)}` }];
  return JSON.stringify(output ?? {}, null, 2);
}

let mdRenderer = null;
function renderMarkdown(value) {
  if (!mdRenderer) {
    mdRenderer = window.markdownit ? window.markdownit({ html: false, linkify: true, breaks: true }) : null;
  }
  if (!mdRenderer) return esc(value || "");
  const text = String(value || "");
  const formulas = [];
  const protectedText = text.replace(/\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$/g, (match, block, inline) => {
    const key = `\uE000M${formulas.length}\uE001`;
    formulas.push({ key, formula: (block !== undefined ? block : inline).trim(), display: block !== undefined });
    return key;
  });
  const html = mdRenderer.render(protectedText);
  let output = html;
  for (const { key, formula, display } of formulas) {
    let rendered;
    try {
      rendered = window.katex ? window.katex.renderToString(fixMathFormula(formula), { displayMode: display, throwOnError: false }) : esc(formula);
    } catch (error) {
      rendered = esc(formula);
    }
    output = output.split(key).join(rendered);
  }
  return output;
}

/** 通用 LCS 序列 diff（单元：行 / 句 / 词） */
function diffSequence(curArr, prevArr) {
  const n = curArr.length;
  const m = prevArr.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = curArr[i] === prevArr[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (curArr[i] === prevArr[j]) {
      ops.push({ type: "same", text: curArr[i] });
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      ops.push({ type: "add", text: curArr[i] });
      i += 1;
    } else {
      ops.push({ type: "del", text: prevArr[j] });
      j += 1;
    }
  }
  while (i < n) {
    ops.push({ type: "add", text: curArr[i] });
    i += 1;
  }
  while (j < m) {
    ops.push({ type: "del", text: prevArr[j] });
    j += 1;
  }
  return ops;
}

/** 按中英文句子切分；保留标点，空白单独成片 */
function splitSentences(text) {
  const raw = String(text || "");
  if (!raw) return [];
  // 句号类切分，标点跟在前一句；换行保留为独立单元避免段落粘连
  const parts = raw.split(/([。！？；;!?]+\s*|\n+)/);
  const out = [];
  for (let k = 0; k < parts.length; k++) {
    const piece = parts[k];
    if (piece === undefined || piece === "") continue;
    if (/^[。！？；;!?]+\s*$/.test(piece) || /^\n+$/.test(piece)) {
      if (out.length) out[out.length - 1] += piece;
      else out.push(piece);
    } else {
      out.push(piece);
    }
  }
  return out.length ? out : [raw];
}

/** 词级（CJK 字 / 英文词）切分，用于同一句内的局部插入 */
function splitWords(text) {
  const raw = String(text || "");
  if (!raw) return [];
  return raw.match(/[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u4e00-\u9fff]|\s+/g) || [raw];
}

function isTrivial(text) {
  return !String(text || "").replace(/\s+/g, "").length;
}

/**
 * 画像 diff：
 * 1) 先按行 LCS
 * 2) 对齐上的「改写行」再按句 diff
 * 3) 仍不对齐的句再按词 diff（只标改动的句子/词，不整段染色）
 */
function diffProfileSmart(curText, prevText) {
  const curLines = String(curText || "").replace(/\r\n/g, "\n").split("\n");
  const prevLines = String(prevText || "").replace(/\r\n/g, "\n").split("\n");
  const lineOps = diffSequence(curLines, prevLines);
  const segments = [];
  let i = 0;
  while (i < lineOps.length) {
    const op = lineOps[i];
    if (op.type === "same") {
      segments.push({ type: "same", text: op.text });
      i += 1;
      continue;
    }
    // 收集连续 add/del 块，尝试配对为“同行改写”
    const adds = [];
    const dels = [];
    while (i < lineOps.length && lineOps[i].type !== "same") {
      if (lineOps[i].type === "add") adds.push(lineOps[i].text);
      else dels.push(lineOps[i].text);
      i += 1;
    }
    // 空行直接透传
    if (!adds.length && dels.every(isTrivial)) {
      for (const d of dels) segments.push({ type: "same", text: d });
      continue;
    }
    if (!dels.length && adds.every(isTrivial)) {
      for (const a of adds) segments.push({ type: "same", text: a });
      continue;
    }
    // 多对多：按顺序 zip 成句级混合段；多余的 add/del 单独挂
    const pairs = Math.min(adds.length, dels.length);
    for (let k = 0; k < pairs; k++) {
      const a = adds[k];
      const d = dels[k];
      if (a === d) {
        segments.push({ type: "same", text: a });
        continue;
      }
      if (isTrivial(a) && isTrivial(d)) {
        segments.push({ type: "same", text: a });
        continue;
      }
      // 句级
      const sentOps = diffSequence(splitSentences(a), splitSentences(d));
      // 若整句几乎全变，再对“改写句”做词级，避免整段删除线
      const refined = [];
      let si = 0;
      while (si < sentOps.length) {
        const sOp = sentOps[si];
        if (sOp.type === "same") {
          refined.push(sOp);
          si += 1;
          continue;
        }
        const sAdds = [];
        const sDels = [];
        while (si < sentOps.length && sentOps[si].type !== "same") {
          if (sentOps[si].type === "add") sAdds.push(sentOps[si].text);
          else sDels.push(sentOps[si].text);
          si += 1;
        }
        const sp = Math.min(sAdds.length, sDels.length);
        for (let t = 0; t < sp; t++) {
          const sa = sAdds[t];
          const sd = sDels[t];
          if (sa === sd) {
            refined.push({ type: "same", text: sa });
            continue;
          }
          // 词级：只在句内标插入/删除
          const wordOps = diffSequence(splitWords(sa), splitWords(sd));
          // 若词级变更比例过高（>70%），退回整句 add+del，避免碎成满屏色块
          const changed = wordOps.filter((w) => w.type !== "same" && !isTrivial(w.text)).length;
          const total = wordOps.filter((w) => !isTrivial(w.text)).length || 1;
          if (changed / total > 0.7) {
            if (!isTrivial(sd)) refined.push({ type: "del", text: sd });
            if (!isTrivial(sa)) refined.push({ type: "add", text: sa });
          } else {
            for (const w of wordOps) {
              if (isTrivial(w.text) && w.type !== "same") {
                // 空白跟在 same 上，避免单独高亮
                if (refined.length) refined[refined.length - 1].text += w.text;
                else refined.push({ type: "same", text: w.text });
              } else {
                refined.push(w);
              }
            }
          }
        }
        for (let t = sp; t < sDels.length; t++) {
          if (!isTrivial(sDels[t])) refined.push({ type: "del", text: sDels[t] });
        }
        for (let t = sp; t < sAdds.length; t++) {
          if (!isTrivial(sAdds[t])) refined.push({ type: "add", text: sAdds[t] });
        }
      }
      segments.push({ type: "mixed", parts: refined, newline: true });
    }
    for (let k = pairs; k < dels.length; k++) {
      if (!isTrivial(dels[k])) segments.push({ type: "del", text: dels[k] });
      else segments.push({ type: "same", text: dels[k] });
    }
    for (let k = pairs; k < adds.length; k++) {
      if (!isTrivial(adds[k])) segments.push({ type: "add", text: adds[k] });
      else segments.push({ type: "same", text: adds[k] });
    }
  }
  return segments;
}

function escInline(text) {
  return esc(String(text || "")).replace(/\n/g, "<br>");
}

function renderProfileDiff(curText, prevText) {
  const cur = String(curText || "").trim();
  const prev = String(prevText || "").trim();
  if (!cur) return renderMarkdown(t("memory.noProfileShort"));
  if (!prev || prev === cur) return renderMarkdown(cur);
  const segments = diffProfileSmart(cur, prev);
  let addCount = 0;
  let delCount = 0;
  const countOps = (ops) => {
    for (const op of ops) {
      if (op.type === "add" && !isTrivial(op.text)) addCount += 1;
      if (op.type === "del" && !isTrivial(op.text)) delCount += 1;
      if (op.type === "mixed" && op.parts) countOps(op.parts);
    }
  };
  countOps(segments);

  const parts = [];
  if (addCount || delCount) {
    parts.push(
      `<div class="profile-diff-legend">` +
      (addCount ? `<span class="prof-legend-add">${t("memory.diffAdd", addCount)}</span>` : "") +
      (delCount ? `<span class="prof-legend-del">${t("memory.diffDel", delCount)}</span>` : "") +
      `<span class="prof-legend-hint">${t("memory.diffHintSentence")}</span></div>`
    );
  }

  // 把 same 行攒成 markdown 块；变更片段用 inline span 插入，避免整段染色
  let sameBuf = [];
  const flushSame = () => {
    if (!sameBuf.length) return;
    const block = sameBuf.join("\n");
    sameBuf = [];
    if (block.trim()) parts.push(`<div class="prof-same">${renderMarkdown(block)}</div>`);
    else if (block.includes("\n")) parts.push(`<div class="prof-same">${"<br>".repeat(block.split("\n").length - 1)}</div>`);
  };

  const renderInlineOps = (ops) => {
    let html = "";
    for (const op of ops) {
      if (op.type === "same") html += escInline(op.text);
      else if (op.type === "add") html += `<span class="prof-ins">${escInline(op.text)}</span>`;
      else if (op.type === "del") html += `<span class="prof-del-inline">${escInline(op.text)}</span>`;
    }
    return html;
  };

  for (const seg of segments) {
    if (seg.type === "same") {
      sameBuf.push(seg.text);
      continue;
    }
    flushSame();
    if (seg.type === "mixed") {
      // 混合行：内联红/绿句，不跑 markdown 以免拆标签
      parts.push(`<p class="prof-line">${renderInlineOps(seg.parts || [])}</p>`);
      continue;
    }
    if (isTrivial(seg.text)) continue;
    if (seg.type === "add") {
      // 纯新增整句/整行：主题色，无删除线
      parts.push(`<p class="prof-line"><span class="prof-ins">${escInline(seg.text)}</span></p>`);
    } else if (seg.type === "del") {
      // 纯删除：主题色 + 删除线
      parts.push(`<p class="prof-line"><span class="prof-del-inline">${escInline(seg.text)}</span></p>`);
    }
  }
  flushSame();
  return parts.join("") || renderMarkdown(cur);
}

function fixMathFormula(formula) {
  return formula
    .replace(/#/g, "\\#")
    .replace(/@/g, "\\text{@}")
    .replace(/×/g, "\\times ")
    .replace(/[\u4e00-\u9fff]+/g, (text) => `\\text{${text}}`);
}

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.style.borderColor = isError ? "var(--danger)" : "var(--line-strong)";
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 3600);
}

const pageTitleKeys = { overview: "page.overview", workers: "page.workers", memory: "page.memory", sop: "page.sop", persona: "page.persona", timeline: "page.timeline", diary: "page.diary", todo: "page.todo", services: "page.services", knowledge: "page.knowledge", events: "page.events", environment: "page.environment", prompt: "page.prompt" };
function pageTitle(page) { const key = pageTitleKeys[page]; return key ? t(key) : ""; }

function setSidebarOpen(open) {
  $("sidebar").classList.toggle("open", open);
  document.body.classList.toggle("sidebar-open", open);
  $("menu-button").setAttribute("aria-expanded", String(open));
}

function routeTo(page) {
  const target = pageTitleKeys[page] ? page : "overview";
  document.querySelectorAll("[data-page-view]").forEach((view) => view.classList.toggle("active", view.dataset.pageView === target));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === target));
  $("page-name").textContent = pageTitle(target);
  document.title = `${pageTitle(target)} · ${t("app.title")}`;
  setSidebarOpen(false);
  window.scrollTo(0, 0);
  if (target === "prompt" && !promptData) loadPrompt();
  if (target === "persona") loadPersona();
  if (target === "timeline" && !timelineData) loadTimeline();
  if (target === "diary" && !diaryData) loadDiary();
  if (target === "sop" && !sopData) loadSopIndex();
  if (target === "environment") { loadModels(); loadModelConfig(); }
  if (target === "services") { renderServicesPage(true); if (terminalId) startTerminalPoll(); }
  else if (terminalTimer) stopTerminalPoll();
  if (target === "workers") startWorkerMonitorPoll();
  else stopWorkerMonitorPoll();
}

function routeFromHash() { routeTo((location.hash || "#overview").slice(1)); }

// 自定义主题:变量清单(shared=通用, mode=分深浅两套)
const THEME_EDITOR_VARS = {
  shared: [
    ["accent-rgb", t("theme.var.accentRgb")], ["wechat", t("theme.var.accent")], ["wechat-strong", t("theme.var.accentDeep")], ["wechat-soft", t("theme.var.accentSoft")], ["line-strong", t("theme.var.accentStroke")], ["radius", t("theme.var.radius")],
    ["danger", t("theme.var.danger")], ["warning", t("theme.var.warn")], ["blue", t("theme.var.blue")], ["purple", t("theme.var.purple")],
    ["cat-life", t("theme.var.catLife")], ["cat-work", t("theme.var.catWork")], ["cat-study", t("theme.var.catStudy")], ["cat-exercise", t("theme.var.catExercise")], ["cat-entertainment", t("theme.var.catFun")],
    ["cat-health", t("theme.var.catHealth")], ["cat-social", t("theme.var.catSocial")], ["cat-care", t("theme.var.catCare")], ["cat-travel", t("theme.var.catTravel")], ["cat-rest", t("theme.var.catRest")],
  ],
  mode: [
    ["bg", t("theme.var.bg")], ["sidebar", t("theme.var.sidebar")], ["panel", t("theme.var.panel")], ["panel-strong", t("theme.var.panelSolid")], ["field", t("theme.var.input")], ["line", t("theme.var.border")],
    ["text", t("theme.var.text")], ["soft", t("theme.var.textSub")], ["muted", t("theme.var.textWeak")], ["faint", t("theme.var.textFaint")], ["overlay", t("theme.var.overlay")], ["shadow", t("theme.var.shadow")],
  ],
};

function applyCustomTheme(theme) {
  let style = $("g4w-custom-theme");
  if (!style) {
    style = document.createElement("style");
    style.id = "g4w-custom-theme";
    document.head.appendChild(style);
  }
  const shared = (theme && theme.shared) || {};
  const dark = (theme && theme.dark) || {};
  const light = (theme && theme.light) || {};
  const block = (vars) => Object.entries(vars).map(([k, v]) => `--${k}:${v};`).join("");
  style.textContent =
    `:root[data-palette="custom"]{${block(shared)}${block(dark)}}` +
    `:root[data-palette="custom"][data-theme="light"]{${block(light)}}`;
}

function applyAppearance(mode, palette) {
  const resolved = mode === "system" ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark") : mode;
  const resolvedPalette = palette === "neko" ? "neko" : palette === "argon" || palette === "blue" ? "argon" : palette === "custom" ? "custom" : "wechat";
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.themeMode = mode;
  document.documentElement.dataset.palette = resolvedPalette;
  if (resolvedPalette === "custom") applyCustomTheme(dashboardData?.configuration?.theme);
  $("theme-icon").textContent = mode === "light" ? "☀" : mode === "dark" ? "☾" : "◐";
  $("theme-button").title = `${t("topbar.theme")} · ${mode === "system" ? t("sidebar.mode.system") : mode === "light" ? t("sidebar.mode.light") : t("sidebar.mode.dark")}`;
  $("theme-mode-select").value = mode;
  document.querySelectorAll("[data-palette-choice]").forEach((button) => button.classList.toggle("active", button.dataset.paletteChoice === resolvedPalette));
  const paletteLabel = t("sidebar.palette." + (["neko", "argon", "custom"].includes(resolvedPalette) ? resolvedPalette : "wechat"));
  const modeLabel = mode === "system" ? t("sidebar.mode.system") : mode === "light" ? t("sidebar.mode.light") : t("sidebar.mode.dark");
  $("appearance-current").textContent = `${paletteLabel} · ${modeLabel}`;
  const metaColor = resolved === "light"
    ? (resolvedPalette === "neko" ? "#fff7f4" : resolvedPalette === "argon" ? "#f2f4f8" : "#f5f7f6")
    : (resolvedPalette === "neko" ? "#181217" : resolvedPalette === "argon" ? "#141a23" : "#111714");
  document.querySelector('meta[name="theme-color"]').content = metaColor;
}

function cycleTheme() {
  const current = document.documentElement.dataset.themeMode || "system";
  const next = current === "system" ? "light" : current === "light" ? "dark" : "system";
  localStorage.setItem("g4w-theme", next);
  applyAppearance(next, document.documentElement.dataset.palette || "wechat");
}

function workerRow(worker, compact = false) {
  if (compact) return `<div class="worker-row" data-worker-id="${esc(worker.id)}"><div class="worker-symbol">◈</div><div class="worker-copy"><strong>${esc(worker.topic)}</strong><span>${esc(worker.summary)}</span></div><div class="worker-meta"><b class="status-${esc(worker.status)}">${statusLabel(worker.status)}</b><small>${esc(worker.model)}</small></div></div>`;
  const outputBtn = (worker.dir || worker.archivePath)
    ? `<button class="worker-output-btn" data-worker-output="${esc(worker.dir)}" data-worker-archive="${esc(worker.archivePath)}" data-worker-id="${esc(worker.id)}" data-worker-label="${esc(worker.topic)}" title="${t("worker.outputBtnTitle")}">${t("worker.outputBtn")}</button>`
    : "";
  return `<div class="data-row clickable" data-worker-id="${esc(worker.id)}"><div class="data-icon">◈</div><div class="data-copy"><strong>${esc(worker.topic)}</strong><span>${esc(worker.summary)}</span><small>${esc(worker.capability || worker.id)} · run ${esc(worker.runIndex)}</small></div><div class="data-meta"><b class="status-${esc(worker.status)}">${statusLabel(worker.status)}</b><span>${esc(worker.model)}</span>${outputBtn}</div></div>`;
}

let themeEditMode = "dark";

function renderThemeEditor() {
  const theme = (dashboardData && dashboardData.configuration && dashboardData.configuration.theme) || {};
  const container = $("theme-editor");
  if (!container) return;
  const mode = themeEditMode === "light" ? "light" : "dark";
  const shared = theme.shared || {};
  const surface = theme[mode] || {};
  const row = (key, label, value, group) =>
    `<label class="theme-row"><span>${label}<small>--${key}</small></span><input type="text" data-theme-var="${key}" data-theme-group="${group}" value="${esc(value)}" placeholder="${t("theme.default")}" spellcheck="false" /></label>`;
  const html =
    `<div class="theme-group-label">${t("theme.groupCommon")}</div>` +
    THEME_EDITOR_VARS.shared.map(([key, label]) => row(key, label, shared[key] || "", "shared")).join("") +
    `<div class="theme-group-label">${t(mode === "dark" ? "theme.modeDark" : "theme.modeLight")}</div>` +
    THEME_EDITOR_VARS.mode.map(([key, label]) => row(key, label, surface[key] || "", mode)).join("");
  container.innerHTML = html;
}

function collectTheme() {
  const theme = { shared: {}, dark: {}, light: {} };
  document.querySelectorAll("[data-theme-var]").forEach((input) => {
    const value = input.value.trim();
    if (!value) return;
    theme[input.dataset.themeGroup][input.dataset.themeVar] = value;
  });
  return theme;
}

async function saveTheme() {
  const messageEl = $("theme-message");
  try {
    const theme = collectTheme();
    const res = await fetchJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ theme }) });
    if (!res.ok) throw new Error(res.error || t("toast.saveFail"));
    if (dashboardData && dashboardData.configuration) dashboardData.configuration.theme = theme;
    if (document.documentElement.dataset.palette === "custom") applyAppearance(document.documentElement.dataset.themeMode || "system", "custom");
    messageEl.textContent = t("theme.saved");
    messageEl.style.color = "";
  } catch (error) {
    messageEl.textContent = error.message || t("toast.saveFail");
    messageEl.style.color = "#ff6b6b";
  }
}

async function resetTheme() {
  const messageEl = $("theme-message");
  try {
    const res = await fetchJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ theme: { dark: {}, light: {}, shared: {} } }) });
    if (!res.ok) throw new Error(res.error || t("toast.restoreFail"));
    if (dashboardData && dashboardData.configuration) dashboardData.configuration.theme = { dark: {}, light: {}, shared: {} };
    renderThemeEditor();
    if (document.documentElement.dataset.palette === "custom") applyAppearance(document.documentElement.dataset.themeMode || "system", "custom");
    messageEl.textContent = t("theme.restored");
    messageEl.style.color = "";
  } catch (error) {
    messageEl.textContent = error.message || t("toast.restoreFail");
    messageEl.style.color = "#ff6b6b";
  }
}

const HERO_GREETING_MAX_LINES = 3;
let heroFitBound = false;

// 问候语长短不一：按行数上限自动缩字号，避免把右侧状态组件挤变形
function fitHeroGreeting() {
  const span = $("hero-greeting");
  const host = span ? span.parentElement : null;
  if (!host) return;
  if (!heroFitBound) {
    heroFitBound = true;
    window.addEventListener("resize", () => {
      window.clearTimeout(fitHeroGreeting._t);
      fitHeroGreeting._t = window.setTimeout(fitHeroGreeting, 150);
    });
  }
  host.style.fontSize = "";
  host.style.lineHeight = "";
  let size = parseFloat(window.getComputedStyle(host).fontSize) || 32;
  for (let i = 0; i < 24 && size > 17; i += 1) {
    if (host.scrollHeight <= size * 1.28 * HERO_GREETING_MAX_LINES + 1) break;
    size -= 1;
    host.style.fontSize = `${size}px`;
    host.style.lineHeight = `${(size * 1.28).toFixed(1)}px`;
  }
}

function renderMaintenanceActions(actions) {
  const host = $("setting-maintenance-actions");
  if (!host) return;
  if (!Array.isArray(actions) || !actions.length) { host.innerHTML = ""; return; }
  host.innerHTML = actions.map((item) => `<label class="switch-row"><span><b>${esc(item.label || item.id)}</b><small>${esc(item.desc || "")}</small></span><input type="checkbox" data-maintenance-action="${esc(item.id)}" ${item.allowed ? "checked" : ""} /></label>`).join("");
}

function renderSettings(config) {
  if (settingsDirty || !config) return;
  const values = {
    "setting-user-name": config.userName,
    "setting-user-identity": config.userIdentity,
    "setting-user-gender": config.userGender,
    "setting-bot-name": config.botName,
    "setting-workspace": config.workspaceRoot,
    "setting-chunk": config.chunkChars,
    "setting-checkin-min": config.checkinMin,
    "setting-checkin-max": config.checkinMax
  };
  Object.entries(values).forEach(([id, value]) => { if ($(id)) $(id).value = value ?? ""; });
  const checks = {
    "setting-turn": config.turnEnabled,
    "setting-worker-turn": config.workerTurnEnabled,
    "setting-maintenance-turn": config.maintenanceTurnEnabled,
    "setting-input": config.inputCaptureEnabled,
    "setting-checkin": config.checkinEnabled,
    "setting-vector": config.vectorEnabled,
    "setting-web-search": config.webSearchEnabled
  };
  Object.entries(checks).forEach(([id, value]) => { if ($(id)) $(id).checked = Boolean(value); });
  renderMaintenanceActions(config.maintenanceActions);
  renderThemeEditor();
  loadEmbeddingConfig();
}

function compactEventRows(items) {
  return items.map((event) => `<div class="event-row"><span class="event-dot ${event.status === "failed" ? "danger" : ""}"></span><div><strong>${esc(event.type)}</strong><span>${esc(event.detail || event.rawType || t("event.systemStateChanged"))}</span></div><time>${esc(event.time)}</time></div>`).join("");
}

function eventIcon(category) {
  return ({ conversation: "✉", worker: "◈", memory: "✦", automation: "◷", system: "⌘" })[category] || "•";
}

function renderSystemEvents(events) {
  const categories = ["worker", "memory"];
  $("events-page-badge").textContent = `${events.length} EVENTS`;
  $("events-worker-count").textContent = events.filter((event) => event.category === "worker").length;
  $("events-memory-count").textContent = events.filter((event) => event.category === "memory").length;
  $("events-error-count").textContent = events.filter((event) => event.status === "failed" || event.status === "pending").length;
  const filtered = eventFilter === "all" ? events : events.filter((event) => event.category === eventFilter);
  $("events-visible-count").textContent = filtered.length;
  $("events-page-list").innerHTML = filtered.length ? filtered.map((event) => {
    const category = categories.includes(event.category) ? event.category : event.category === "conversation" ? "conversation" : "";
    return `<article class="system-event"><div class="system-event-icon ${esc(category)}">${eventIcon(event.category)}</div><div class="system-event-copy"><strong>${esc(event.type)}</strong><span>${esc(event.detail || t("event.systemStateChanged"))}</span><small>${esc(event.rawType || "system.event")}${event.relatedId ? ` · ${esc(event.relatedId)}` : ""}</small></div><div class="system-event-meta"><b>${esc(statusLabel(event.status || "done"))}</b>${esc(event.time || "--")}</div></article>`;
  }).join("") : '<div class="empty-state">' + t("empty.events.filtered") + '</div>';
}

function renderKnowledgeCatalog(items) {
  const tagSelect = $("knowledge-tag");
  const currentOptions = new Set(Array.from(tagSelect.options).map((option) => option.value));
  const tags = [...new Set(items.flatMap((item) => item.tags || []).filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b), "zh-CN"));
  if (tags.some((tag) => !currentOptions.has(String(tag))) || tagSelect.options.length !== tags.length + 1) {
    tagSelect.innerHTML = '<option value="">' + t("knowledge.allTags") + '</option>' + tags.map((tag) => `<option value="${esc(tag)}">${esc(tag)}</option>`).join("");
    tagSelect.value = knowledgeTag;
  }
  const query = knowledgeQuery.trim().toLowerCase();
  const visible = items.filter((item) => {
    const haystack = [item.title, item.id, ...(item.tags || [])].join(" ").toLowerCase();
    return (!query || haystack.includes(query)) && (!knowledgeTag || (item.tags || []).includes(knowledgeTag));
  });
  $("knowledge-page-list").innerHTML = visible.length ? visible.map((item) => `<div class="data-row clickable" data-knowledge-id="${esc(item.id)}"><div class="data-icon blue">▣</div><div class="data-copy"><strong>${esc(item.title)}</strong><span>${(item.tags || []).map((tag) => `<i class="tag">${esc(tag)}</i>`).join("") || t("knowledge.noTag")}</span><small>${esc(item.id)}</small></div><div class="data-meta"><b>${esc(item.chunks)} CHUNKS</b><span>${esc(item.updatedAt)}</span></div></div>`).join("") : '<div class="empty-state">' + t("empty.knowledge.filtered") + '</div>';
}

function render(data) {
  dashboardData = data;
  hydrateUiPrefs(data);
  const system = data.system || fallback.system;
  const metrics = data.metrics || fallback.metrics;
  if (document.documentElement.dataset.palette === "custom") applyCustomTheme(dashboardData?.configuration?.theme);
  const memory = data.memory || fallback.memory;
  const knowledge = data.knowledge || fallback.knowledge;
  const configuration = data.configuration || fallback.configuration;
  const workers = data.workers || [];
  const events = data.events || [];
  const online = system.status === "online";

  const greeting = system.greeting || {};
  const rawGreeting = String(greeting.text || "").trim() || t("hero.greetingHello", system.user || t("kpi.localUser"));
  // 服务端已限长（greeting.py MAX_TEXT_CHARS），这里再挡一层异常超长文本
  const greetingText = rawGreeting.length > 80 ? `${rawGreeting.slice(0, 80).trimEnd()}…` : rawGreeting;
  if ($("hero-greeting")) {
    $("hero-greeting").textContent = greetingText;
    $("hero-greeting").title = greeting.source === "conductor" ? t("hero.greetingSourceConductor") : t("hero.greetingFallback");
    fitHeroGreeting();
  }
  $("top-status").textContent = statusLabel(system.status);
  $("hero-status").textContent = `CORE ${statusLabel(system.status)}`;
  $("hero-model").textContent = `${system.model || t("state.modelUnset")} · ${system.vector ? "VECTOR ON" : "KEYWORD MODE"}`;
  $("conductor-node").textContent = online ? "ONLINE" : "STANDBY";
  document.querySelector(".node-core").classList.toggle("online", online);
  $("running-workers").textContent = metrics.runningWorkers ?? 0;
  $("worker-total").textContent = `${t("metric.registered", metrics.workers ?? 0)}`;
  $("memory-count").textContent = metrics.memoryProfiles ?? 0;
  $("memory-backups").textContent = `${t("metric.backups", metrics.memoryBackups ?? 0)}`;
  $("knowledge-count").textContent = knowledge.documents ?? metrics.knowledgeDocuments ?? 0;
  $("chunk-count").textContent = `${t("metric.chunks", knowledge.chunks ?? metrics.knowledgeChunks ?? 0)}`;
  $("event-count").textContent = metrics.pendingEvents ?? 0;
  $("worker-node").textContent = `${metrics.runningWorkers ?? 0} ACTIVE`;
  document.querySelector(".node-worker").classList.toggle("busy", (metrics.runningWorkers ?? 0) > 0);
  $("memory-node").textContent = `${metrics.memoryProfiles ?? 0} PROFILES`;
  $("kb-node").textContent = `${knowledge.documents ?? 0} DOCS`;
  $("context-model").textContent = system.model || "--";
  $("context-vector").textContent = system.vector ? t("state.enabled") : t("state.disabled");
  $("context-memory").textContent = memory.root || "--";
  $("context-kb").textContent = knowledge.root || "--";
  $("last-updated").textContent = fmtTime((data.updatedAt || Date.now()/1000)*1000);
  const nextCheckin = Number(data.nextCheckinAt) || 0;
  const nextCheckinEl = $("next-checkin-time");
  if (nextCheckinEl) {
    nextCheckinEl.textContent = nextCheckin > 0
      ? fmtHM(nextCheckin * 1000)
      : t("time.notScheduled");
  }
  $("footer-year").textContent = new Date().getFullYear();
  $("sidebar-status").textContent = t("state.online");

  $("worker-list").innerHTML = workers.length ? workers.slice(0,8).map((worker) => workerRow(worker, true)).join("") : '<div class="empty-state">' + t("empty.workers.short") + '</div>';
  $("workers-page-total").textContent = metrics.workers ?? workers.length;
  $("workers-page-running").textContent = metrics.runningWorkers ?? 0;
  $("workers-page-visible").textContent = workers.length;
  $("workers-page-badge").textContent = `${metrics.workers ?? workers.length} REGISTERED`;
  $("worker-page-list").innerHTML = workers.length ? workers.map((worker) => workerRow(worker)).join("") : '<div class="empty-state">' + t("empty.workers.none") + '</div>';
  $("worker-page-list").querySelectorAll("[data-worker-output]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      selectWorkerMonitor(btn.dataset.workerOutput, btn.dataset.workerArchive, btn.dataset.workerId, btn.dataset.workerLabel);
    });
  });

  $("event-list").innerHTML = events.length ? compactEventRows(events.slice(0,8)) : '<div class="empty-state">' + t("empty.events.none") + '</div>';
  renderSystemEvents(events);

  renderMemoryPage(memory.items || []);

  renderTodoPage();
  renderServicesPage();
  updateOverviewStartButton();

  const knowledgeItems = knowledge.items || [];
  $("knowledge-page-total").textContent = knowledge.documents ?? 0;
  $("knowledge-page-chunks").textContent = knowledge.chunks ?? 0;
  $("knowledge-page-vector").textContent = knowledge.vector ? "ON" : "OFF";
  $("knowledge-page-badge").textContent = `${knowledge.documents ?? 0} DOCUMENTS`;
  renderKnowledgeCatalog(knowledgeItems);

  renderSettings(configuration);

  // 时间轴/日记加载失败后自动重试：连接拥堵导致的超时中止（"signal is aborted
  // without reason"）不应永久残留错误——SSE 每秒驱动，5 秒节流，成功后自然停止
  const activeView = document.querySelector('[data-page-view].active')?.dataset.pageView;
  const nowMs = Date.now();
  if (activeView === "timeline" && !timelineData && nowMs - lastTimelineAttempt > 5000) loadTimeline();
  if (activeView === "diary" && !diaryData && nowMs - lastDiaryAttempt > 5000) loadDiary();
  if (activeView === "sop" && !sopData && nowMs - lastSopAttempt > 5000) loadSopIndex();
}

async function fetchJson(url, options) {
  // 超时兜底 + 超时后自动重试一次（仅 GET：防止重启看板后浏览器/代理
  // 复用指向旧进程的死 keep-alive 连接导致请求挂起；重试会走新连接）
  const attempt = async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, { cache: "no-store", signal: controller.signal, ...options });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
      return payload;
    } finally {
      clearTimeout(timer);
    }
  };
  try {
    return await attempt();
  } catch (error) {
    const isGet = !options || !options.method || options.method === "GET";
    if (isGet && error.name === "AbortError") {
      return attempt(); // 重试一次走新连接
    }
    throw error;
  }
}

async function refresh() {
  try { render(await fetchJson("/api/dashboard")); }
  catch (error) { if (!dashboardData) render(fallback); console.warn("G4W dashboard refresh failed", error); }
}

  function currentPage() {
    return document.querySelector("[data-page-view].active")?.dataset.pageView || "overview";
  }

  function markRefreshed(label) {
    const now = fmtTime(Date.now());
    const stamp = $("refresh-stamp");
    const last = $("last-updated");
    if (stamp) stamp.textContent = now;
    if (last) last.textContent = now;
    showToast(`${t("toast.refreshed", label, now)}`);
    const view = document.querySelector("[data-page-view].active");
    if (view) {
      view.classList.remove("just-refreshed");
      void view.offsetWidth;
      view.classList.add("just-refreshed");
    }
  }

  function resetPageCaches() {
    timelineData = null;
    diaryData = null;
    sopData = null;
    currentSopPath = "";
    promptData = null;
    currentMemoryId = "";
    currentMemoryData = null;
    todoLastFetchAt = 0;
    servicesCache = null;
    servicesCacheAt = 0;
    servicesSignature = "";
    embeddingLastLoadAt = 0;
    const conductor = $("setting-conductor-model");
    if (conductor) conductor.dataset.loaded = "";
  }


  async function refreshActivePage() {
    const page = document.querySelector("[data-page-view].active")?.dataset.pageView || "overview";
    if (page === "timeline") { timelineData = null; return loadTimeline(); }
    if (page === "diary") {
      await loadDiary();
      if (currentDiaryDate) return openDiary(currentDiaryDate);
      return;
    }
    if (page === "sop") {
      await loadSopIndex(true);
      if (currentSopPath) return openSop(currentSopPath);
      return;
    }
    if (page === "memory") {
      const id = $("memory-profile-select")?.value || currentMemoryId;
      currentMemoryId = "";
        if (id) return loadMemoryDetail(id);
      return;
    }
    if (page === "todo") { todoLastFetchAt = 0; return renderTodoPage(true); }
    if (page === "services") { servicesCache = null; servicesCacheAt = 0; servicesSignature = ""; return renderServicesPage(true); }
    if (page === "prompt") { promptData = null; return loadPrompt(); }
    if (page === "environment") {
      await loadModels(true);
      embeddingLastLoadAt = 0;
        const conductor = $("setting-conductor-model");
        if (conductor) conductor.dataset.loaded = "";
        return loadEmbeddingConfig(true);
      }
      if (page === "workers") {
        workerMonitorCursor = 0;
        return startWorkerMonitorPoll();
    }
  }

  async function refreshNow() {
    const button = $("refresh-button");
    if (button) {
      button.classList.add("is-refreshing");
      button.disabled = true;
    }
    try {
      await refresh();
      const stamp = $("last-updated");
      if (stamp) stamp.textContent = fmtTime(Date.now());
        markRefreshed(pageTitle(currentPage()));
      await refreshActivePage();
    } catch (error) {
      console.warn("G4W dashboard manual refresh failed", error);
      showToast(error.message || t("toast.refreshFail"), true);
    } finally {
      if (button) {
        if (overlay) overlay.hidden = true;
          button.classList.remove("is-refreshing");
        button.disabled = false;
      }
    }
  }

  async function refreshNowFixed() {
    const button = $("refresh-button");
    const overlay = $("refresh-overlay");
    const page = currentPage();
    const label = pageTitle(page);
    if (button) {
      button.classList.add("is-refreshing");
      button.disabled = true;
    }
    if (overlay) overlay.hidden = false;
    try {
      resetPageCaches();
      await refresh();
      await refreshActivePage();
      markRefreshed(label);
    } catch (error) {
      if (!dashboardData) render(fallback);
      console.warn("G4W dashboard manual refresh failed", error);
      showToast(error.message || t("toast.refreshFail"), true);
    } finally {
      if (overlay) overlay.hidden = true;
      if (button) {
        button.classList.remove("is-refreshing");
        button.disabled = false;
      }
    }
  }

const timelineCategories = {
  life: "cat.life", work: "cat.work", study: "cat.study", exercise: "cat.exercise", entertainment: "cat.entertainment",
  health: "cat.health", social: "cat.social", care: "cat.care", travel: "cat.travel", rest: "cat.rest"
};
function categoryLabel(id) { return timelineCategories[id] ? t(timelineCategories[id]) : (id || t("cat.other")); }

function dateObject(value) { return new Date(`${value}T12:00:00`); }
function isoDate(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
function addDays(value, amount) { const date = dateObject(value); date.setDate(date.getDate() + amount); return isoDate(date); }
function shortDate(value) { return fmtShortDate(value); }
function weekday(value) { return fmtWeekday(dateObject(value).getTime()); }
function timelineColor(category) { return `var(--cat-${timelineCategories[category] ? category : "life"})`; }
function eventClock(value) { return String(value || "").slice(11, 16) || "--:--"; }
function clockMinutes(value) {
  const [hour, minute] = eventClock(value).split(":").map(Number);
  return Math.max(0, Math.min(1440, (hour || 0) * 60 + (minute || 0)));
}
function eventMinutes(event) {
  const start = new Date(event.startAt).getTime();
  const end = new Date(event.endAt).getTime();
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(1, Math.round((end - start) / 60000)) : 1;
}
function formatDuration(minutes) {
  if (minutes < 60) return `${minutes}m`;
  const hours = minutes / 60;
  return `${hours >= 10 || Number.isInteger(hours) ? hours.toFixed(0) : hours.toFixed(1)}h`;
}
function rangeDates() {
  if (!timelineDate) return [];
  if (timelineMode === "day") return [timelineDate];
  if (timelineMode === "week") {
    const selected = dateObject(timelineDate);
    const offset = (selected.getDay() + 6) % 7;
    const monday = addDays(timelineDate, -offset);
    return Array.from({ length: 7 }, (_, index) => addDays(monday, index));
  }
  const selected = dateObject(timelineDate);
  const first = new Date(selected.getFullYear(), selected.getMonth(), 1, 12);
  const last = new Date(selected.getFullYear(), selected.getMonth() + 1, 0, 12);
  return Array.from({ length: last.getDate() }, (_, index) => isoDate(new Date(first.getFullYear(), first.getMonth(), index + 1, 12)));
}
function eventsForDates(dates) {
  return dates.flatMap((date) => (((timelineData || {}).facts || {})[date]?.events || []).map((event) => ({ ...event, date })));
}
function hourLabels() {
  return `<div class="hour-labels">${Array.from({ length: 24 }, (_, hour) => `<span class="hour-label" style="top:${hour * timelineSlotHeight}px">${fmtHourLabel(hour)}</span>`).join("")}</div>`;
}
function timelineTooltipText(event) {
  const category = categoryLabel(event.categoryId);
  return `${event.date}|${eventClock(event.startAt)}–${eventClock(event.endAt)} · ${formatDuration(eventMinutes(event))}|${category}|${event.note || t("timeline.noNote")}`;
}
function timelineBlock(event) {
  const dayHeight = timelineSlotHeight * 24;
  const top = clockMinutes(event.startAt) / 60 * timelineSlotHeight;
  const height = Math.max(24, Math.min(dayHeight - top, eventMinutes(event) / 60 * timelineSlotHeight));
  return `<button class="timeline-event-block" data-timeline-tooltip="${esc(timelineTooltipText(event))}" data-timeline-event="${esc(event.id)}" data-timeline-date="${esc(event.date)}" style="top:${top}px;height:${height}px;--event-color:${timelineColor(event.categoryId)}"><strong>${esc(event.title)}<span class="block-duration"> | ${formatDuration(eventMinutes(event))}</span></strong><small>${eventClock(event.startAt)}–${eventClock(event.endAt)} · ${esc(timelineCategories[event.categoryId] || event.categoryId || t("cat.other"))}</small></button>`;
}
function dayBoard(dates) {
  const date = dates[0];
  const events = filterTimelineEvents(eventsForDates(dates));
  return `<div class="timeline-scroll"><div class="day-board" style="--slot-height:${timelineSlotHeight}px;--day-height:${timelineSlotHeight * 24}px">${hourLabels()}<div class="timeline-lane">${events.map(timelineBlock).join("")}</div></div></div>`;
}
function weekBoard(dates) {
  const heads = dates.map((date) => `<div class="week-day-head"><div>${weekday(date)}<small>${shortDate(date)}</small></div></div>`).join("");
  const lanes = dates.map((date) => `<div class="week-lane">${filterTimelineEvents(eventsForDates([date])).map(timelineBlock).join("")}</div>`).join("");
  return `<div class="timeline-scroll"><div class="week-board" style="--slot-height:${timelineSlotHeight}px;--day-height:${timelineSlotHeight * 24}px"><div></div>${heads}${hourLabels()}${lanes}</div></div>`;
}
function monthBoard(dates) {
  const rows = dates.map((date) => {
    const events = filterTimelineEvents(eventsForDates([date]));
    const blocks = events.map((event) => {
      const left = clockMinutes(event.startAt) / 14.4;
      const width = Math.max(.5, Math.min(100 - left, eventMinutes(event) / 14.4));
      return `<button class="month-event" data-timeline-tooltip="${esc(timelineTooltipText(event))}" data-timeline-event="${esc(event.id)}" data-timeline-date="${esc(date)}" style="left:${left}%;width:${width}%;--event-color:${timelineColor(event.categoryId)}"><span>${esc(eventClock(event.startAt))} ${esc(event.title)}</span></button>`;
    }).join("");
    return `<div class="month-row"><div class="month-day">${shortDate(date)}<small>${t("timeline.dayCount", weekday(date), events.length)}</small></div><div class="month-track">${blocks}</div></div>`;
  }).join("");
  return `<div class="timeline-scroll"><div class="month-board" style="min-width:${Math.round(820 * timelineMonthZoom)}px">${rows}</div></div>`;
}
function renderTimeline() {
  if (!timelineData || !timelineDate) return;
  const dates = rangeDates();
  const allEvents = eventsForDates(dates).sort((a, b) => String(a.startAt).localeCompare(String(b.startAt)));
  const events = filterTimelineEvents(allEvents);
  const minutes = events.reduce((total, event) => total + eventMinutes(event), 0);
  const categoryMinutes = {};
  allEvents.forEach((event) => { categoryMinutes[event.categoryId || "life"] = (categoryMinutes[event.categoryId || "life"] || 0) + eventMinutes(event); });
  const categories = Object.entries(categoryMinutes).sort((a, b) => b[1] - a[1]);
  const rangeLabel = dates.length === 1 ? dates[0] : `${dates[0].slice(5)} → ${dates.at(-1).slice(5)}`;
  $("timeline-page-badge").textContent = `${timelineData.metrics?.days || timelineData.dates.length} DAYS`;
  $("timeline-range").textContent = rangeLabel;
  $("timeline-events-count").textContent = events.length;
  $("timeline-duration").textContent = formatDuration(minutes);
  $("timeline-category-count").textContent = categories.length;
  $("timeline-updated").textContent = timelineData.metrics?.updatedAt || "--";
  $("timeline-focus-title").textContent = timelineMode === "day" ? `${t("timeline.focus.day", shortDate(timelineDate))}` : timelineMode === "week" ? t("timeline.focus.week") : `${t("timeline.focus.month", timelineDate.slice(0, 7))}`;
  $("timeline-focus-subtitle").textContent = timelineMode === "day" ? t("timeline.focus.subDay") : timelineMode === "week" ? t("timeline.focus.subWeek") : t("timeline.focus.subMonth");
  $("timeline-zoom-label").textContent = timelineMode === "month" ? `${Math.round(timelineMonthZoom * 100)}%` : `${Math.round(timelineSlotHeight / 30 * 100)}%`;
  $("timeline-board").innerHTML = allEvents.length ? (timelineMode === "day" ? dayBoard(dates) : timelineMode === "week" ? weekBoard(dates) : monthBoard(dates)) : '<div class="empty-state">' + t("timeline.emptyRange") + '</div>';
  $("timeline-legend").innerHTML = categories.map(([category]) => `<span class="legend-chip"><i style="background:${timelineColor(category)}"></i>${esc(categoryLabel(category))}</span>`).join("");
  let cursor = 0;
  const allMinutes = allEvents.reduce((total, event) => total + eventMinutes(event), 0);
  $("timeline-donut").innerHTML = renderDonut(categories, allMinutes);
  $("timeline-donut").dataset.total = formatDuration(allMinutes);
  const filterLabel = timelineCategoryFilter ? categoryLabel(timelineCategoryFilter) : t("timeline.all");
  const pillColor = timelineCategoryFilter ? timelineColor(timelineCategoryFilter) : "";
  ["timeline-filter-tag", "timeline-trend-tag", "timeline-events-tag"].forEach((id) => {
    const tag = $(id);
    if (!tag) return;
    tag.hidden = !timelineCategoryFilter;
    tag.textContent = filterLabel;
    tag.style.setProperty("--pill-color", pillColor);
  });
  $("timeline-distribution").innerHTML = categories.length ? categories.map(([category, value]) => {
    const active = timelineCategoryFilter === category;
    const dimmed = timelineCategoryFilter && !active;
    return `<button class="distribution-row ${active ? "active" : ""} ${dimmed ? "dimmed" : ""}" data-timeline-filter="${esc(category)}"><i style="background:${timelineColor(category)}"></i><span>${esc(categoryLabel(category))}</span><small>${formatDuration(value)} · ${Math.round(value / (allEvents.reduce((total, event) => total + eventMinutes(event), 0) || 1) * 100)}%</small></button>`;
  }).join("") : '<div class="empty-state">' + t("timeline.emptyCategories") + '</div>';
  const dayTotals = dates.map((date) => filterTimelineEvents(eventsForDates([date])).reduce((total, event) => total + eventMinutes(event), 0));
  const peak = Math.max(1, ...dayTotals);
  const barColor = timelineCategoryFilter ? timelineColor(timelineCategoryFilter) : "";
  const gridMax = Math.max(1, Math.ceil(peak / 60));
  const gridFmt = (hours) => hours === 0 ? "0" : hours < 1 ? `${Math.round(hours * 60)}m` : hours % 1 === 0 ? `${hours}h` : `${hours.toFixed(1)}h`;
  const gridLines = [0, 1, 2, 3, 4].map((i) => `<div class="trend-grid-line" style="bottom:${i * 25}%"><span>${gridFmt(gridMax * i / 4)}</span></div>`).join("");
  $("timeline-trend").innerHTML = `<div class="trend-grid">${gridLines}</div>` + dates.map((date, index) => `<div class="trend-bar" style="height:${Math.max(2, dayTotals[index] / peak * 100)}%;${barColor ? `--bar-color:${barColor};` : ""}" data-value="${formatDuration(dayTotals[index])}"><span>${timelineMode === "month" ? date.slice(8) : shortDate(date)}</span></div>`).join("");
  $("timeline-event-list").innerHTML = events.length ? events.map((event) => `<button class="timeline-event-card" data-timeline-event="${esc(event.id)}" data-timeline-date="${esc(event.date)}" style="--event-color:${timelineColor(event.categoryId)};--event-mix:${timelineMix(event)}%"><time>${esc(event.date)} · ${eventClock(event.startAt)}–${eventClock(event.endAt)}</time><strong>${esc(event.title)}<span class="block-duration"> | ${formatDuration(eventMinutes(event))}</span></strong><span>${esc(event.note || `${categoryLabel(event.categoryId)} · ${formatDuration(eventMinutes(event))}`)}</span></button>`).join("") : '<div class="empty-state">' + t("timeline.emptyEvents") + '</div>';
}

function changeTimelineZoom(direction, anchorEvent = null) {
  const scroll = $("timeline-board").querySelector(".timeline-scroll");
  const rect = scroll?.getBoundingClientRect();
  const x = rect && anchorEvent ? Math.max(0, anchorEvent.clientX - rect.left) : (scroll?.clientWidth || 0) / 2;
  const y = rect && anchorEvent ? Math.max(0, anchorEvent.clientY - rect.top) : (scroll?.clientHeight || 0) / 2;
  const beforeWidth = scroll?.scrollWidth || 1;
  const beforeHeight = scroll?.scrollHeight || 1;
  const anchorX = ((scroll?.scrollLeft || 0) + x) / beforeWidth;
  const anchorY = ((scroll?.scrollTop || 0) + y) / beforeHeight;
  if (timelineMode === "month") {
    timelineMonthZoom = direction === 0 ? 1 : Math.max(1, Math.min(4, timelineMonthZoom + direction * .25));
  } else {
    timelineSlotHeight = direction === 0 ? 30 : Math.max(20, Math.min(92, timelineSlotHeight + direction * 4));
  }
  renderTimeline();
  const next = $("timeline-board").querySelector(".timeline-scroll");
  if (next) {
    next.scrollLeft = Math.max(0, anchorX * next.scrollWidth - x);
    next.scrollTop = Math.max(0, anchorY * next.scrollHeight - y);
  }
}

function showTimelineTooltip(target, clientX, clientY) {
  const parts = String(target.dataset.timelineTooltip || "").split("|");
  const tooltip = $("timeline-tooltip");
  const eventColor = getComputedStyle(target).getPropertyValue("--event-color").trim();
  if (eventColor) tooltip.style.setProperty("--tip-color", eventColor);
  tooltip.innerHTML = `<strong>${esc(target.querySelector("strong,span")?.textContent || t("timeline.eventTitle"))}</strong><span>${esc(parts.slice(0, 3).filter(Boolean).join(" · "))}</span><span>${esc(parts.slice(3).join("|") || t("timeline.noNote"))}</span>`;
  tooltip.hidden = false;
  const width = tooltip.offsetWidth;
  const height = tooltip.offsetHeight;
  tooltip.style.left = `${Math.max(12, Math.min(window.innerWidth - width - 12, clientX + 14))}px`;
  tooltip.style.top = `${Math.max(12, Math.min(window.innerHeight - height - 12, clientY + 14))}px`;
}

function hideTimelineTooltip() { $("timeline-tooltip").hidden = true; }

async function loadTimeline() {
  lastTimelineAttempt = Date.now();
  $("timeline-board").innerHTML = '<div class="empty-state">' + t("timeline.loading") + '</div>';
  try {
    timelineData = await fetchJson("/api/timeline");
    timelineDate = timelineDate || timelineData.latestDate || timelineData.dates.at(-1) || "";
    $("timeline-date").innerHTML = [...timelineData.dates].reverse().map((date) => `<option value="${date}">${date} · ${weekday(date)}</option>`).join("");
    $("timeline-date").value = timelineDate;
    renderTimeline();
  } catch (error) { $("timeline-board").innerHTML = `<div class="empty-state">${esc(error.message)}</div>`; }
}

function openTimelineEvent(eventId, date) {
  const event = ((((timelineData || {}).facts || {})[date] || {}).events || []).find((item) => String(item.id) === String(eventId));
  if (!event) return;
  const tags = (event.tags || []).map((tag) => `<span>${esc(tag)}</span>`).join("");
  const html = `<div class="detail-pills"><span>${esc(date)}</span><span>${eventClock(event.startAt)}–${eventClock(event.endAt)}</span><span>${formatDuration(eventMinutes(event))}</span><span>${esc(timelineCategories[event.categoryId] || event.categoryId || t("cat.other"))}</span>${tags}</div><section class="drawer-section"><h3>${t("timeline.record")}</h3><p>${esc(event.note || t("timeline.noBlockNote"))}</p></section><section class="drawer-section"><h3>${t("timeline.category")}</h3><p>${esc(event.subcategoryId || event.categoryId || t("timeline.uncategorized"))}</p></section>`;
  openDrawer(event.title || t("timeline.eventTitle"), "TIMELINE EVENT", html, timelineColor(event.categoryId));
}

function renderDiaryIndex() {
  const entries = (diaryData || {}).entries || [];
  const query = $("diary-search").value.trim().toLowerCase();
  const visible = entries.filter((entry) => (!diaryMonth || entry.date.startsWith(diaryMonth)) && (!query || [entry.date, entry.title, entry.excerpt].join(" ").toLowerCase().includes(query)));
  const monthCount = entries.filter((entry) => !diaryMonth || entry.date.startsWith(diaryMonth)).length;
  $("diary-page-badge").textContent = `${monthCount} / ${entries.length} DAYS`;
  $("diary-date-list").innerHTML = visible.length ? visible.map((entry) => `<button class="diary-date ${entry.date === currentDiaryDate ? "active" : ""}" data-diary-date="${entry.date}"><time>${entry.date}</time><strong>${esc(entry.title || t("diary.dayTitle"))}</strong><span>${esc(entry.excerpt || "")}</span></button>`).join("") : '<div class="empty-state">' + t("diary.noMatch") + '</div>';
}

function renderDiaryMonths() {
  const counts = {};
  for (const entry of (diaryData?.entries || [])) counts[entry.date.slice(0, 7)] = (counts[entry.date.slice(0, 7)] || 0) + 1;
  const months = Object.keys(counts).sort().reverse();
  diaryMonth = diaryMonth || months[0] || "";
  $("diary-month").innerHTML = months.map((month) => {
    const [year, number] = month.split("-");
    return `<option value="${month}">${t("diary.monthOption", year, Number(number), counts[month])}</option>`;
  }).join("");
  $("diary-month").value = diaryMonth;
}

async function loadDiary() {
  lastDiaryAttempt = Date.now();
  try {
    diaryData = await fetchJson("/api/diary");
    renderDiaryMonths();
    renderDiaryIndex();
    const first = diaryData.entries?.find((entry) => entry.date.startsWith(diaryMonth));
    if (!currentDiaryDate && first) await openDiary(first.date);
  } catch (error) { $("diary-date-list").innerHTML = `<div class="empty-state">${esc(error.message)}</div>`; }
}

async function openDiary(date) {
  currentDiaryDate = date;
  diaryMonth = date.slice(0, 7);
  if ($("diary-month")) $("diary-month").value = diaryMonth;
  renderDiaryIndex();
  $("diary-title").textContent = t("persona.loading");
  $("diary-content").innerHTML = '<div class="empty-state">' + t("state.loading") + '</div>';
  try {
    const data = await fetchJson(`/api/diary?date=${encodeURIComponent(date)}`);
    const entry = (diaryData?.entries || []).find((item) => item.date === date);
    $("diary-title").textContent = entry?.title || `${t("diary.titleWithDate", date)}`;
    $("diary-meta").innerHTML = [`${t("diary.metaDate", date)}`, `${t("diary.sectionCount", data.sections || 0)}`, data.updatedAt || "--", data.source || ""].filter(Boolean).map((value) => `<span>${esc(value)}</span>`).join("");
    $("diary-content").innerHTML = renderMarkdown(data.content || t("diary.empty"));
    const index = (diaryData?.entries || []).findIndex((item) => item.date === date);
    $("diary-prev").disabled = index >= (diaryData?.entries || []).length - 1;
    $("diary-next").disabled = index <= 0;
  } catch (error) { $("diary-title").textContent = t("diary.readFail"); $("diary-content").innerHTML = `<div class="empty-state">${esc(error.message)}</div>`; }
}

function moveDiary(direction) {
  const entries = diaryData?.entries || [];
  const index = entries.findIndex((entry) => entry.date === currentDiaryDate);
  const target = entries[index + direction];
  if (target) openDiary(target.date);
}

function visibleSopFiles() {
  const files = (sopData || {}).files || [];
  const query = ($("sop-search")?.value || "").trim().toLowerCase();
  if (!query) return files;
  return files.filter((file) => [file.path, file.title, file.name, file.folder, file.excerpt].join(" ").toLowerCase().includes(query));
}

/** 构建目录树：{ name, path, dirs: Map, files: [] } */
function buildSopTree(files) {
  const root = { name: "", path: "", dirs: new Map(), files: [] };
  for (const file of files) {
    const parts = String(file.path || "").split("/").filter(Boolean);
    if (!parts.length) continue;
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const name = parts[i];
      const dirPath = parts.slice(0, i + 1).join("/");
      if (!node.dirs.has(name)) node.dirs.set(name, { name, path: dirPath, dirs: new Map(), files: [] });
      node = node.dirs.get(name);
    }
    node.files.push(file);
  }
  return root;
}

function countSopTreeFiles(node) {
  let n = (node.files || []).length;
  for (const child of (node.dirs || new Map()).values()) n += countSopTreeFiles(child);
  return n;
}

function ensureSopPathExpanded(relPath) {
  const parts = String(relPath || "").split("/").filter(Boolean);
  for (let i = 1; i < parts.length; i++) sopExpanded.add(parts.slice(0, i).join("/"));
}

function expandSopSearchMatches(files, query) {
  if (!query) return;
  for (const file of files) {
    const hay = [file.path, file.title, file.name, file.folder, file.excerpt].join(" ").toLowerCase();
    if (hay.includes(query)) ensureSopPathExpanded(file.path);
  }
}

function renderSopTreeHtml(node, depth) {
  const rows = [];
  const dirNames = [...node.dirs.keys()].sort((a, b) => a.localeCompare(b, "zh"));
  for (const name of dirNames) {
    const child = node.dirs.get(name);
    const open = sopExpanded.has(child.path);
    const count = countSopTreeFiles(child);
    rows.push(
      `<button type="button" class="sop-dir ${open ? "open" : ""}" data-sop-dir="${esc(child.path)}" style="--sop-depth:${depth}">` +
      `<span class="sop-caret">${open ? "▾" : "▸"}</span>` +
      `<span class="sop-dir-icon">📁</span>` +
      `<strong>${esc(child.name)}</strong>` +
      `<span class="sop-count">${count}</span></button>`
    );
    if (open) rows.push(renderSopTreeHtml(child, depth + 1));
  }
  const files = [...(node.files || [])].sort((a, b) => (a.name || "").localeCompare(b.name || "", "zh"));
  for (const file of files) {
    const active = file.path === currentSopPath ? "active" : "";
    const ext = (file.ext || "").toUpperCase() || "FILE";
    rows.push(
      `<button type="button" class="sop-file ${active}" data-sop-path="${esc(file.path)}" style="--sop-depth:${depth}" title="${esc(file.path)}">` +
      `<span class="sop-file-icon">📄</span>` +
      `<span class="sop-file-main"><strong>${esc(file.name)}</strong>` +
      `<small>${esc(ext)}${file.title && file.title !== file.name ? " · " + esc(file.title) : ""}</small></span></button>`
    );
  }
  return rows.join("");
}

function renderSopIndex() {
  const files = visibleSopFiles();
  const total = ((sopData || {}).files || []).length;
  if ($("sop-page-badge")) $("sop-page-badge").textContent = `${files.length}${files.length !== total ? ` / ${total}` : ""} FILES`;
  const list = $("sop-file-list");
  if (!list) return;
  if (!files.length) {
    const scopeName = (sopData && sopData.scope === "sop-user") ? t("sop.scopeUser") : t("sop.scopeShared");
    list.innerHTML = `<div class="empty-state">${t("sop.emptyScope", scopeName)}</div>`;
    return;
  }
  const query = ($("sop-search")?.value || "").trim().toLowerCase();
  // 搜索时自动展开命中路径；打开文件时展开其祖先目录
  if (query) expandSopSearchMatches(files, query);
  if (currentSopPath) ensureSopPathExpanded(currentSopPath);
  const tree = buildSopTree(files);
  list.innerHTML = `<div class="sop-tree">${renderSopTreeHtml(tree, 0)}</div>`;
}

function toggleSopDir(dirPath) {
  const key = String(dirPath || "");
  if (sopExpanded.has(key)) sopExpanded.delete(key);
  else sopExpanded.add(key);
  renderSopIndex();
}

async function loadSopIndex(force = false) {
  lastSopAttempt = Date.now();
  const scopeSelect = $("sop-scope");
  if (scopeSelect && !force) {
    if ([...scopeSelect.options].some((opt) => opt.value === sopScope)) scopeSelect.value = sopScope;
  }
  try {
    sopData = await fetchJson(`/api/sop?scope=${encodeURIComponent(sopScope || "sop")}`);
    if (scopeSelect && Array.isArray(sopData.scopes) && sopData.scopes.length) {
      // 下拉始终提供「个人 SOP + 共享 SOP」两项（后端缺项时补一个"不存在"的占位）
      const stats = Object.fromEntries(sopData.scopes.map((item) => [String(item.id), item]));
      const scopes = ["sop-user", "sop"].map((id) => stats[id] || { id: id, exists: false, count: 0 });
      scopeSelect.innerHTML = scopes.map((item) => {
        const label = item.id === "sop-user" ? t("sop.scopeUser") : t("sop.scopeShared");
        const count = item.exists ? ` · ${item.count}` : " · " + t("state.missing") + "";
        return `<option value="${esc(item.id)}">${esc(label + count)}</option>`;
      }).join("");
      // v3.0.3：默认落「共享 SOP」；只有个人 SOP 确实存在且有文件时才默认个人 SOP
      const usable = (id) => !!(stats[id] && stats[id].exists && Number(stats[id].count) > 0);
      const preferred = usable("sop-user") ? "sop-user" : "sop";
      if (!usable(sopScope) || !stats[sopScope]) sopScope = preferred;
      localStorage.setItem("g4w-sop-scope", sopScope);
      scopeSelect.value = sopScope;
      if (String(sopData.scope) !== sopScope) {
        sopData = await fetchJson(`/api/sop?scope=${encodeURIComponent(sopScope)}`);
      }
    }
    // 切换目录时收起展开状态；若有当前文件则只展开其祖先
    if (force) sopExpanded = new Set();
    if (currentSopPath) ensureSopPathExpanded(currentSopPath);
    renderSopIndex();
    const files = visibleSopFiles();
    // 不自动打开第一份，避免一进页就铺开阅读区；仅在已有选中失效时换一篇
    if (currentSopPath && files.length && !files.some((f) => f.path === currentSopPath)) {
      await openSop(files[0].path);
    }
  } catch (error) {
    if ($("sop-file-list")) $("sop-file-list").innerHTML = `<div class="empty-state">${esc(error.message)}</div>`;
  }
}

async function openSop(relPath) {
  currentSopPath = relPath;
  renderSopIndex();
  if ($("sop-title")) $("sop-title").textContent = t("persona.loading");
  if ($("sop-content")) $("sop-content").innerHTML = '<div class="empty-state">' + t("state.loading") + '</div>';
  try {
    const data = await fetchJson(`/api/sop?scope=${encodeURIComponent(sopScope)}&path=${encodeURIComponent(relPath)}`);
    const entry = ((sopData || {}).files || []).find((item) => item.path === relPath);
    if ($("sop-title")) $("sop-title").textContent = data.title || entry?.title || data.name || relPath;
    if ($("sop-meta")) {
      $("sop-meta").innerHTML = [
        data.ext ? String(data.ext).toUpperCase() : "",
        data.path || relPath,
        data.sections ? `${t("diary.sectionCount", data.sections)}` : "",
        data.bytes != null ? formatBytes(data.bytes) : "",
        data.updatedAt || "",
        data.source || "",
      ].filter(Boolean).map((value) => `<span>${esc(value)}</span>`).join("");
    }
    if ($("sop-content")) {
      // md 直接渲染；json/txt/py 服务端已包代码围栏
      $("sop-content").innerHTML = renderMarkdown(data.content || t("sop.emptyFile"));
    }
    const files = visibleSopFiles();
    const index = files.findIndex((item) => item.path === relPath);
    if ($("sop-prev")) $("sop-prev").disabled = index <= 0;
    if ($("sop-next")) $("sop-next").disabled = index < 0 || index >= files.length - 1;
  } catch (error) {
    if ($("sop-title")) $("sop-title").textContent = t("sop.readFail");
    if ($("sop-content")) $("sop-content").innerHTML = `<div class="empty-state">${esc(error.message)}</div>`;
  }
}

function moveSop(direction) {
  const files = visibleSopFiles();
  const index = files.findIndex((item) => item.path === currentSopPath);
  const target = files[index + direction];
  if (target) openSop(target.path);
}

async function openKnowledge(documentId) {
  openDrawer(t("knowledge.loadingDoc"), "KNOWLEDGE DETAIL", '<div class="empty-state">' + t("knowledge.loadingBody") + '</div>');
  try {
    const data = await fetchJson(`/api/knowledge?id=${encodeURIComponent(documentId)}`);
    const chunks = (data.chunks || []).map((chunk, index) => `<article class="chunk-card"><header><span>#${index + 1}${chunk.section ? ` · ${esc(chunk.section)}` : ""}</span><span>${chunk.page != null ? `PAGE ${esc(chunk.page)}` : esc(chunk.id)}</span></header><p>${esc(chunk.text)}${chunk.truncated ? "\n…" : ""}</p></article>`).join("") || '<div class="empty-state">' + t("knowledge.noChunks") + '</div>';
    const sourceRows = [[t("knowledge.docId"), data.id], [t("knowledge.sourceFile"), data.sourcePath], [t("knowledge.storedFile"), data.storedPath], [t("knowledge.createdAt"), data.createdAt], [t("knowledge.updatedAt"), data.updatedAt], [t("knowledge.fileSize"), formatBytes(data.bytes)], [t("knowledge.chunks"), `${data.loadedChunks} / ${data.chunkCount}`]].map(([label, value]) => `<div class="source-row"><span>${esc(label)}</span><code>${esc(value || "--")}</code></div>`).join("");
    const warning = data.extractionWarning ? `<div class="knowledge-warning">${esc(data.extractionWarning)}</div>` : "";
    const isPdf = data.format === "pdf";
    const pdfUrl = `/api/knowledge/pdf?id=${encodeURIComponent(data.id)}`;
    const contentTab = isPdf ? `<div class="pdf-toolbar"><a class="secondary-button" href="${pdfUrl}" target="_blank" rel="noopener">${t("knowledge.openPdf")}</a></div><iframe class="pdf-viewer" src="${pdfUrl}" title="${t("knowledge.pdfPreview")}"></iframe>` : `${warning}<div class="markdown">${renderMarkdown(data.content || t("knowledge.bodyEmpty"))}</div>${data.contentTruncated ? '<div class="empty-state">' + t("knowledge.bodyTruncated") + '</div>' : ""}`;
    const html = `<div class="detail-pills">${(data.tags || []).map((tag) => `<span>${esc(tag)}</span>`).join("")}<span>${data.chunkCount || 0} CHUNKS</span><span>${formatBytes(data.bytes)}</span><span>${esc(String(data.contentSource || "stored").toUpperCase())}</span></div><div class="knowledge-tabs"><button class="active" data-knowledge-tab="content">${isPdf ? t("knowledge.pdfRaw") : t("knowledge.extractBody")}</button><button data-knowledge-tab="chunks">${t("knowledge.chunks")}</button><button data-knowledge-tab="info">${t("knowledge.infoTab")}</button></div><section class="knowledge-panel" data-knowledge-panel="content">${contentTab}</section><section class="knowledge-panel" data-knowledge-panel="chunks" hidden>${chunks}${data.loadedChunks < data.chunkCount ? `<div class="empty-state">${t("knowledge.chunkLimit", data.loadedChunks)}</div>` : ""}</section><section class="knowledge-panel" data-knowledge-panel="info" hidden><div class="source-table">${sourceRows}</div></section>`;
    openDrawer(data.title || documentId, "KNOWLEDGE DETAIL", html);
  } catch (error) { openDrawer(t("knowledge.readFail"), "ERROR", `<div class="empty-state">${esc(error.message)}</div>`); }
}

let dashboardEvents = null;
let dashboardReconnectTimer = null;
function disconnectEvents() {
  if (dashboardReconnectTimer) clearTimeout(dashboardReconnectTimer);
  dashboardReconnectTimer = null;
  if (dashboardEvents) dashboardEvents.close();
  dashboardEvents = null;
}
function connectEvents() {
  if (!("EventSource" in window)) return;
  disconnectEvents();
  const source = new EventSource("/api/events");
  dashboardEvents = source;
  source.addEventListener("dashboard", (event) => { try { render(JSON.parse(event.data)); } catch (error) { console.warn("Invalid dashboard event", error); } });
  source.onerror = () => {
    source.close();
    if (dashboardEvents !== source) return;
    dashboardEvents = null;
    dashboardReconnectTimer = setTimeout(async () => {
      dashboardReconnectTimer = null;
      if (await ensureAuth()) connectEvents();
    }, 3000);
  };
}

function openDrawer(title, eyebrow, html, accentVar) {
  $("drawer-title").textContent = title;
  $("drawer-eyebrow").textContent = eyebrow;
  $("drawer-body").innerHTML = html;
  const layer = $("drawer-layer");
  if (accentVar) layer.style.setProperty("--drawer-accent", `var(${accentVar})`);
  else layer.style.removeProperty("--drawer-accent");
  layer.hidden = false;
  document.body.classList.add("drawer-open");
}

function closeDrawer() { $("drawer-layer").hidden = true; document.body.classList.remove("drawer-open"); }

async function openWorker(workerId) {
  openDrawer(t("worker.loading"), "WORKER DETAIL", '<div class="empty-state">' + t("state.loading") + '</div>');
  try {
    const data = await fetchJson(`/api/worker?id=${encodeURIComponent(workerId)}`);
    const artifacts = (data.runs || []).flatMap((run) => run.artifacts || []);
    const artifactHtml = artifacts.length ? artifacts.map((item) => `<div class="artifact"><div><strong>${esc(item.name)}</strong><small>${esc(item.path)} · ${formatBytes(item.bytes)}</small></div>${item.exists ? `<a href="/api/file?path=${encodeURIComponent(item.path)}">${t("worker.download")}</a>` : '<span class="muted-tag">' + t("state.fileMissing") + '</span>'}</div>`).join("") : '<div class="empty-state">' + t("worker.noArtifacts") + '</div>';
    const runsHtml = (data.runs || []).map((run, index) => `<details class="accordion" ${index === 0 ? "open" : ""}><summary><span>${esc(run.name)} · ${statusLabel(run.status)}</span><span>${esc(run.updatedAt)}</span></summary><div class="accordion-body">${run.report ? `<div class="markdown">${renderMarkdown(run.report)}</div>` : ""}<pre class="json-view">${esc(jsonText(run.result || run.progress || {}))}</pre></div></details>`).join("") || '<div class="empty-state">' + t("worker.legacyRun") + '</div>';
    const html = `<div class="detail-pills"><span>${esc(data.status)}</span><span>${esc(data.capability || "general")}</span><span>${esc(data.model)}</span><span>run ${esc(data.runIndex)}</span><span>${esc(data.updatedAt)}</span></div><section class="drawer-section"><h3>${t("worker.sectionTask")}</h3><p>${esc(data.task || data.topic)}</p></section><section class="drawer-section"><h3>${t("worker.lastReply")}</h3><div class="markdown">${renderMarkdown(data.report || data.summary || t("worker.noFinalReply"))}</div></section><section class="drawer-section"><h3>${t("worker.sectionArtifacts")}</h3>${artifactHtml}</section><section class="drawer-section"><h3>${t("worker.sectionRuns")}</h3>${runsHtml}</section><details class="accordion"><summary><span>${t("worker.rawResult")}</span><span>JSON</span></summary><div class="accordion-body"><pre class="json-view">${esc(jsonText(data.result || {}))}</pre></div></details>`;
    openDrawer(data.topic, "WORKER DETAIL", html);
  } catch (error) { openDrawer(t("worker.readFail"), "ERROR", `<div class="empty-state">${esc(error.message)}</div>`); }
}

function memoryRecord(item) {
  if (!item || typeof item !== "object") return `<pre class="json-view">${esc(jsonText(item))}</pre>`;
  const title = item.project || item.fact || item.capability || item.lesson || item.name || item.title || t("memory.entry");
  const description = item.description || item.snippet || item.trigger || item.status || "";
  const meta = [item.category, item.timestamp || item.last_seen || item.learned_at, item.confidence != null ? `${t("memory.confidence", item.confidence)}` : ""].filter(Boolean).join(" · ");
  return `<div class="drawer-section"><h3>${esc(title)}</h3>${description ? `<p>${esc(description)}</p>` : ""}${meta ? `<div class="detail-pills"><span>${esc(meta)}</span></div>` : ""}</div>`;
}

function renderMemoryValue(value, depth = 0) {
  if (Array.isArray(value)) {
    const visible = value.slice(0, 80).map(memoryRecord).join("");
    return visible + (value.length > 80 ? `<div class="empty-state">${t("memory.moreCollapsed", value.length - 80)}</div>` : "");
  }
  if (value && typeof value === "object") {
    return Object.entries(value).map(([key, child]) => {
      const count = Array.isArray(child) ? `${t("unit.items", child.length)}` : child && typeof child === "object" ? `${t("unit.fields", Object.keys(child).length)}` : t("memory.countOne");
      const body = depth < 2 ? renderMemoryValue(child, depth + 1) : `<pre class="json-view">${esc(jsonText(child, 30))}</pre>`;
      return `<details class="accordion"><summary><span>${esc(key.replaceAll("_", " "))}</span><span>${count}</span></summary><div class="accordion-body">${body}</div></details>`;
    }).join("");
  }
  return `<p>${esc(value ?? "")}</p>`;
}

let memoryProfiles = [];
let currentMemoryId = "";
let currentMemorySections = [];
let currentMemoryData = null;
let backupsExpanded = false;
let timelinePoints = [];

function expandBackups(button) {
  backupsExpanded = !backupsExpanded;
  if (currentMemoryData) renderMemoryDetail(currentMemoryData);
}

function openMemorySection(key) {
  const section = currentMemorySections.find((item) => item.key === key);
  if (!section) return;
  openDrawer(section.label, "MEMORY SECTION", renderMemoryValue(section.items));
}

function renderMemoryPage(items) {
  memoryProfiles = items;
  const select = $("memory-profile-select");
  const saved = localStorage.getItem("g4w-memory-profile");
  const current = items.some((item) => item.id === saved) ? saved : (items[0] ? items[0].id : "");
  select.innerHTML = items.length ? items.map((item) => `<option value="${esc(item.id)}">${esc(item.name)}${item.identity ? ` · ${esc(item.identity)}` : ""}</option>`).join("") : '<option value="">' + t("memory.noProfileOption") + '</option>';
  select.disabled = !items.length;
  select.value = current;
  if (current && current !== currentMemoryId) loadMemoryDetail(current);
  else if (!current) $("memory-page-body").innerHTML = '<div class="empty-state glass-card">' + t("memory.noProfile") + '</div>';
}

// ---------- G4W 程序更新（环境配置页卡片；独立挂载，不侵入原页面切换逻辑） ----------
// 顶栏时钟：以前 app.js 里没有任何地方写 #refresh-stamp（只写了 #last-updated），
// 顶栏永远停在 HTML 占位符 --:--:--。这里给它一个每秒走字的本地时间。
function tickTopbarClock() {
  const el = $("refresh-stamp");
  if (el) el.textContent = fmtTime(Date.now());
}
tickTopbarClock();
setInterval(tickTopbarClock, 1000);

let updateCardBound = false;

async function loadUpdateStatus() {
  const el = $("update-status");
  if (!el) return;
  try {
    const data = await fetchJson("/api/update/status");
    el.innerHTML = `<span class="status-running">${t("update.currentVersion", esc(String(data.current || t("update.unknown"))))}</span>` +
      (data.appliedAt ? `<span class="muted-tag">${esc(String(data.appliedAt))}</span>` : "") +
      (data.sourceFile ? `<span class="muted-tag">${t("prompt.sourceFile", esc(String(data.sourceFile)))}</span>` : "");
  } catch (e) {
    el.innerHTML = `<span class="status-stopped">${t("update.versionFail", esc(String((e && e.message) || e)))}</span>`;
  }
}

async function checkUpdate() {
  const detail = $("update-detail");
  const msgEl = $("update-message");
  const btn = $("update-check");
  if (btn) btn.disabled = true;
  if (detail) detail.innerHTML = '<div class="muted-tag">' + t("update.querying") + '</div>';
  if (msgEl) msgEl.textContent = t("update.querying");
  try {
    const mirrorEl = $("update-mirror");
    const mirror = mirrorEl && !mirrorEl.checked ? "0" : "1";
    try { localStorage.setItem("g4w-update-mirror", mirror); } catch (e) { /* 忽略存储失败 */ }
    const data = await fetchJson(`/api/update/check?mirror=${mirror}`);
    if (!data.ok) {
      if (detail) detail.innerHTML = "";
      if (msgEl) {
        msgEl.textContent = `${t("update.checkFailPrefix")}${data.error || t("toast.unknownError")}${data.hint ? t("common.semicolon") + data.hint : ""}`;
      }
      return;
    }
    const assets = (data.assets || []).length
      ? (data.assets || []).map((a) => `${esc(a.name)} · ${formatBytes(a.size)}`).join("<br>")
      : t("update.noAssets");
    if (detail) {
      detail.innerHTML =
        `<div class="embedding-speedtest-row"><span>${t("update.latest")}</span><b>${esc(String(data.latest || "--"))}</b></div>` +
        `<div class="embedding-speedtest-row"><span>${t("update.publishedAt")}</span><b>${esc(String(data.publishedAt || "--"))}</b></div>` +
        `<div class="embedding-speedtest-row"><span>${t("update.assetsLabel")}</span><b>${assets}</b></div>`;
    }
    if ($("update-release")) $("update-release").dataset.url = data.notesUrl || "";
    const applyBtn = $("update-apply");
    if (applyBtn) {
      applyBtn.disabled = !data.hasUpdate;
      applyBtn.title = data.hasUpdate ? `${t("update.updateTo", data.latest)}` : t("update.upToDate");
    }
    if (msgEl) {
      msgEl.textContent = data.hasUpdate
        ? `${t("update.foundVersion", data.latest)}`
        : `${t("update.upToDateWith", data.current || data.latest || "--")}`;
    }
  } catch (e) {
    if (msgEl) msgEl.textContent = t("update.checkFailPrefix") + String((e && e.message) || e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

let updatePollTimer = null;

function setUpdateMessage(text) {
  const el = $("update-message");
  if (el) el.textContent = text;
}

function showUpdateProgress(on) {
  const w = $("update-progress-wrap");
  if (w) w.hidden = !on;
}

async function applyUpdate() {
  const btn = $("update-apply");
  if (!confirm(t("confirm.update"))) return;
  if (btn) btn.disabled = true;
  const mirrorEl = $("update-mirror");
  const mirror = mirrorEl && !mirrorEl.checked ? "0" : "1";
  showUpdateProgress(true);
  setUpdateMessage(t("update.preparing"));
  try {
    const r = await fetchJson(`/api/update/apply?mirror=${mirror}`);
    if (!r.ok) {
      setUpdateMessage((r.error || t("update.cannotStart")) + (r.hint ? t("common.semicolon") + r.hint : ""));
      if (btn) btn.disabled = false;
      return;
    }
    setUpdateMessage(`${t("update.updatingTo", r.version || t("update.newVersion"))}`);
    pollUpdateProgress();
  } catch (e) {
    setUpdateMessage(t("update.failPrefix") + String((e && e.message) || e));
    if (btn) btn.disabled = false;
  }
}

function pollUpdateProgress() {
  if (updatePollTimer) clearInterval(updatePollTimer);
  updatePollTimer = setInterval(async () => {
    const bar = $("update-progress-bar");
    const txt = $("update-progress-text");
    try {
      const s = await fetchJson("/api/update/progress");
      const pct = Math.max(0, Math.min(100, Number(s.progress) || 0));
      if (bar) bar.style.width = `${pct}%`;
      if (txt) txt.textContent = `${s.stage || "working"} · ${pct}% · ${s.message || ""}`;
      if (s.stage === "failed") {
        clearInterval(updatePollTimer);
        updatePollTimer = null;
        setUpdateMessage(t("update.failPrefix") + (s.message || t("toast.unknownError")));
        const b = $("update-apply");
        if (b) b.disabled = false;
      } else if (s.stage === "handoff") {
        setUpdateMessage(t("update.handoff"));
      }
    } catch (e) {
      // 看板正在被更新脚本重启 → 轮询断开属于预期
      if (bar) bar.style.width = "100%";
      if (txt) txt.textContent = t("update.restarting");
      clearInterval(updatePollTimer);
      updatePollTimer = null;
    }
  }, 1200);
}

function initUpdateCard() {
  if (!updateCardBound) {
    updateCardBound = true;
    // 「国内镜像加速」开关：与向量检索安装同一套语义（开=gh-proxy 镜像，关=直连）
    let savedMirror = null;
    try { savedMirror = localStorage.getItem("g4w-update-mirror"); } catch (e) { /* 忽略 */ }
    if ($("update-mirror") && savedMirror !== null) $("update-mirror").checked = savedMirror === "1";
    $("update-mirror")?.addEventListener("change", () => {
      try { localStorage.setItem("g4w-update-mirror", $("update-mirror").checked ? "1" : "0"); } catch (e) { /* 忽略 */ }
    });
    $("update-check")?.addEventListener("click", checkUpdate);
    $("update-apply")?.addEventListener("click", applyUpdate);
    $("update-release")?.addEventListener("click", () => {
      const url = $("update-release")?.dataset.url || "https://github.com/MoFrom-FG/G4W/releases/latest";
      window.open(url, "_blank");
    });
  }
  if (location.hash.replace("#", "") === "environment") loadUpdateStatus();
}
window.addEventListener("hashchange", () => {
  if (location.hash.replace("#", "") === "environment") initUpdateCard();
});
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initUpdateCard);
else initUpdateCard();

// ---------- Embedding 配置 / 测速 / 安装（环境配置页） ----------
let embeddingLastLoadAt = 0;

async function loadEmbeddingConfig(force = false) {
  const nowMs = Date.now();
  if (!force && nowMs - embeddingLastLoadAt < 10000) return;
  embeddingLastLoadAt = nowMs;
  const message = $("embedding-message");
  try {
    const data = await fetchJson("/api/embedding/config");
    const status = data.status || {};
    const statusEl = $("embedding-status");
    if (statusEl) {
      statusEl.innerHTML = status.installed
        ? `<span class="status-running">● ${t("embedding.installed")} (${esc(status.mode || "")}${status.enabled ? " · " + t("state.enabled") + "" : " · " + t("state.disabled") + ""})</span>`
        : `<span class="status-stopped">○ ${t("embedding.notInstalled")} (${t("embedding.keywordOnly")})</span>`;
    }
    const network = (data.config || {}).network || "domestic";
    const net = $("embedding-network");
    if (net) net.checked = network !== "abroad";
    if (message) message.textContent = network === "domestic"
      ? t("embedding.networkDomestic")
      : t("embedding.networkGlobal");
  } catch (error) {
    if (message) message.textContent = `${t("common.readFailPrefix", error.message)}`;
  }
}

async function runEmbeddingSpeedtest() {
  const box = $("embedding-speedtest-result");
  const message = $("embedding-message");
  if (!box) return;
  box.innerHTML = '<div class="empty-state">' + t("embedding.testing") + '</div>';
  try {
    const data = await fetchJson("/api/embedding/speedtest", { method: "POST" });
    if (!data.ok) {
      box.innerHTML = `<div class="empty-state">${t("embedding.testFailPrefix", esc(data.error || t("toast.unknownError")))}</div>`;
      if (message) message.textContent = t("embedding.testFail");
      return;
    }
    const rows = [];
    const kindLabel = { pip: t("embedding.srcPip"), torch: t("embedding.srcTorch"), hf: t("embedding.srcModel") };
    for (const [kind, items] of Object.entries(data.results || {})) {
      rows.push(`<div class="speedtest-kind">${kindLabel[kind] || kind}${data.picked && data.picked[kind] ? `${t("embedding.picked", esc(data.picked[kind]))}` : ""}</div>`);
      (items || []).forEach((item) => {
        const best = data.picked && data.picked[kind] === item.url;
        const cls = best ? "speedtest-row best" : "speedtest-row";
        const latency = item.reachable ? `${item.latency_ms} ms` : t("embedding.unreachable");
        rows.push(`<div class="${cls}"><span>${esc(item.name)}</span><code>${esc(item.url)}</code><b>${latency}${best ? " ★" : ""}</b></div>`);
      });
    }
    box.innerHTML = rows.join("") || '<div class="empty-state">' + t("embedding.noMirrors") + '</div>';
    if (message) message.textContent = `${t("embedding.done", data.cached ? t("embedding.cached") : "")}`;
  } catch (error) {
    box.innerHTML = `<div class="empty-state">${t("embedding.testFailDetail", esc(error.message))}</div>`;
    if (message) message.textContent = t("embedding.testFailAuth");
  }
}

async function startEmbeddingInstall() {
  if (!confirm(t("confirm.embeddingInstall"))) return;
  const message = $("embedding-message");
  try {
    const data = await fetchJson("/api/embedding/install", { method: "POST" });
    if (!data.ok) {
      if (message) message.textContent = t("common.startFailPrefix", data.error || t("toast.unknownError"));
      return;
    }
    if (message) message.textContent = `${t("embedding.taskStarted", data.pid || "?")}`;
    loadEmbeddingConfig(true);
  } catch (error) {
    if (message) message.textContent = `${t("common.startFailPrefix", error.message)}`;
  }
}

// 绑定直接放顶层：app.js 在 body 尾部执行（DOM 已就绪），不依赖 DOMContentLoaded
// （避免事件时序问题导致绑定失效——实证：部分浏览器/WebView 下 DOMContentLoaded 回调不执行）
const net = $("embedding-network");
if (net) net.addEventListener("change", async () => {
  const message = $("embedding-message");
  const network = net.checked ? "domestic" : "abroad";
  try {
    await fetchJson("/api/embedding/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ network }),
    });
    if (message) message.textContent = `${t("embedding.switched", network === "domestic" ? t("embedding.domestic") : t("embedding.global"))}`;
  } catch (error) {
    if (message) message.textContent = `${t("common.saveFailPrefix", error.message)}`;
  }
});
const btnLog = $("embedding-log");
if (btnLog) btnLog.addEventListener("click", () => {
  // 前往「服务与终端」页并选中安装任务服务（实时日志在服务页终端）
  selectTerminal("embedding-install");
  location.hash = "#services";
});
$("embedding-speedtest").addEventListener("click", runEmbeddingSpeedtest);
$("embedding-install").addEventListener("click", startEmbeddingInstall);

// ---------- 待办页（与微信 /todo 同源；标签页 + 分页） ----------
let todoTab = "pending";
let todoPage = 1;
let todoLastFetchAt = 0;
const TODO_PAGE_SIZE = 15;

async function renderTodoPage(force = false) {
  const now = Date.now();
  // SSE 每秒触发 render()：待办数据变化慢，5 秒节流即可；交互（tab/翻页/完成/删除）用 force 强制刷新
  if (!force && now - todoLastFetchAt < 5000) return;
  todoLastFetchAt = now;
  let data;
  try {
    data = await fetchJson("/api/todo");
  } catch (error) {
    $("todo-page-list").innerHTML = `<div class="empty-state">${t("todo.readFail", esc(error.message))}</div>`;
    return;
  }
  const tasks = data.tasks || {};
  const rows = [];
  Object.entries(tasks).forEach(([sender, items]) => {
    (items || []).forEach((task) => rows.push({ sender, task }));
  });
  const pendingRows = rows.filter((r) => r.task.status === "pending");
  const doneRows = rows.filter((r) => r.task.status === "done");
  $("todo-page-pending").textContent = pendingRows.length;
  $("todo-page-overdue").textContent = pendingRows.filter((r) => r.task.classify === "overdue").length;
  $("todo-page-done").textContent = doneRows.length;
  $("todo-page-badge").textContent = `${pendingRows.length} PENDING`;
  const active = todoTab === "done" ? doneRows : pendingRows;
  const totalPages = Math.max(1, Math.ceil(active.length / TODO_PAGE_SIZE));
  if (todoPage > totalPages) todoPage = totalPages;
  const pageRows = active.slice((todoPage - 1) * TODO_PAGE_SIZE, todoPage * TODO_PAGE_SIZE);
  const listEl = $("todo-page-list");
  if (!pageRows.length) {
    listEl.innerHTML = todoTab === "done"
      ? '<div class="empty-state">' + t("todo.emptyDone") + '</div>'
      : '<div class="empty-state">' + t("todo.emptyOpen") + '</div>';
  } else {
    const bySender = {};
    pageRows.forEach(({ sender, task }) => {
      (bySender[sender] = bySender[sender] || []).push(task);
    });
    listEl.innerHTML = Object.entries(bySender)
      .map(([sender, items]) => `<div class="todo-group"><div class="todo-group-label">${esc(sender)}</div>${items.map((t) => todoRow(sender, t)).join("")}</div>`)
      .join("");
    listEl.querySelectorAll("[data-todo-act]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const { sender, id, todoAct: act } = btn.dataset;
        btn.disabled = true;
        try {
          await fetchJson(`/api/todo?action=${encodeURIComponent(act)}&sender=${encodeURIComponent(sender)}&id=${encodeURIComponent(id)}`);
        } catch (error) {
          alert(error.message);
        }
        renderTodoPage(true);
      });
    });
  }
  const info = $("todo-page-info");
  if (info) info.textContent = `${t("todo.pageInfoCount", todoPage, totalPages, active.length)}`;
  const prevBtn = $("todo-prev");
  const nextBtn = $("todo-next");
  if (prevBtn) prevBtn.disabled = todoPage <= 1;
  if (nextBtn) nextBtn.disabled = todoPage >= totalPages;
  document.querySelectorAll("[data-todo-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.todoTab === todoTab);
  });
}

function todoRow(sender, task) {
  const state = task.status === "done" ? "done" : task.classify;
  const labels = { overdue: t("todo.state.overdue"), scheduled: t("todo.state.scheduled"), ongoing: t("todo.state.ongoing"), done: t("todo.state.done") };
  const due = task.dueLabel ? ` <span class="muted">· ${esc(task.dueLabel)}</span>` : "";
    const created = task.createdLabel ? ` <span class="muted">· ${t("todo.created", esc(task.createdLabel))}</span>` : "";
    const completed = task.completedLabel ? ` <span class="muted">· ${t("todo.completed", esc(task.completedLabel))}</span>` : "";
    const overdue = task.status !== "done" && task.overdueDays > 0 ? ` <span class="muted">· ${t("todo.overdueDays", esc(String(task.overdueDays)))}</span>` : "";
  const recur = task.recurLabel ? ` <span class="muted">· ${esc(task.recurLabel)}</span>` : "";
  const fire = task.fireIndex > 0 ? ` <span class="muted">· ${t("todo.fireCount", task.fireIndex)}</span>` : "";
  const del = `<button class="todo-act" data-todo-act="delete" data-sender="${esc(sender)}" data-id="${esc(task.id)}" title="${t("persona.delete")}">🗑</button>`;
  const act = task.status === "pending"
    ? `<button class="todo-act" data-todo-act="done" data-sender="${esc(sender)}" data-id="${esc(task.id)}" title="${t("todo.markDone")}">✅</button>${del}`
    : del;
  return `<div class="todo-item todo-${state}"><span class="todo-badge">${labels[state] || ""}</span><span class="todo-text">${esc(task.text)}</span><span class="todo-meta">${due}${recur}${fire}${created}${completed}${overdue}</span><span class="todo-actions">${act}</span></div>`;
}

// ---- 服务与终端（服务启停 + 实时日志终端） ----
let servicesCache = null;
let servicesCacheAt = 0;
let servicesSignature = "";
let terminalId = "";
let terminalCursor = 0;
let terminalTimer = null;
let terminalPaused = false;
let terminalLastError = "";
let workerMonitorCursor = 0;
let workerMonitorTimer = null;
let workerMonitorLastError = "";
let workerMonitorFileId = "";
let workerMonitorProgressKey = "";
let workerMonitorWorkerDir = "";
let workerMonitorWorkerId = "";

async function loadServices(force = false) {
  const now = Date.now();
  if (!force && servicesCache && now - servicesCacheAt < 3000) return servicesCache;
  try {
    servicesCache = await fetchJson("/api/services");
  } catch (error) {
    servicesCache = servicesCache || { ok: true, services: [] };
  }
  servicesCacheAt = now;
  return servicesCache;
}

function serviceRow(svc) {
  const running = svc.status === "running";
  const actions = svc.managed
    ? `<button class="service-act" data-service-start="${esc(svc.id)}" ${running ? "disabled" : ""}>${t("btn.start")}</button><button class="service-act danger" data-service-stop="${esc(svc.id)}" ${running ? "" : "disabled"}>${t("btn.stop")}</button>`
    : '<span class="muted-tag">' + t("tag.dashboardSelf") + '</span>';
  return `<div class="service-item" data-service-select="${esc(svc.id)}" title="${t("services.selectHint")}">
    <div class="service-head">
      <span class="service-dot svc-${running ? "running" : "stopped"}"></span>
      <div class="service-info"><strong>${esc(svc.name)}</strong><small>${esc(svc.desc || svc.id)}</small></div>
      <span class="service-state ${running ? "is-running" : ""}">${running ? t("state.running") : t("state.stopped")}</span>
    </div>
    <div class="service-foot"><code>${esc(svc.detail || "")}</code><span class="service-actions">${actions}</span></div>
  </div>`;
}

async function renderServicesPage(force = false) {
  const pageActive = !!document.querySelector('[data-page-view="services"]')?.classList.contains("active");
  const data = await loadServices(force);
  const services = data.services || [];
  const running = services.filter((s) => s.status === "running").length;
  const badge = $("services-page-badge");
  if (badge) badge.textContent = `${running}/${services.length} RUNNING`;
  const listEl = $("services-page-list");
  if (!listEl) return;
  if (!pageActive) return; // 非本页时仅刷新缓存（供总览按钮用）
  if (!services.length) {
    listEl.innerHTML = '<div class="empty-state">' + t("services.none") + '</div>';
    return;
  }
  const signature = JSON.stringify(services.map(({ id, status }) => [id, status]));
  if (signature !== servicesSignature) {
    servicesSignature = signature;
    listEl.innerHTML = services.map(serviceRow).join("");
    listEl.querySelectorAll("[data-service-start]").forEach((btn) => {
      btn.addEventListener("click", async (event) => {
        event.stopPropagation();
        btn.disabled = true;
        try {
          const res = await fetchJson(`/api/services?action=start&id=${encodeURIComponent(btn.dataset.serviceStart)}`);
          showToast(res.message || `${t("toast.started")}${res.pid ? t("common.pidInline", res.pid) : ""}`);
        } catch (error) {
          showToast(error.message, true);
        }
        renderServicesPage(true);
        updateOverviewStartButton(true);
      });
    });
    listEl.querySelectorAll("[data-service-stop]").forEach((btn) => {
      btn.addEventListener("click", async (event) => {
        event.stopPropagation();
        btn.disabled = true;
        try {
          const res = await fetchJson(`/api/services?action=stop&id=${encodeURIComponent(btn.dataset.serviceStop)}`);
          showToast(res.actions && res.actions.length ? `${t("services.stoppedWithActions", res.actions.length)}` : t("state.stopped"));
        } catch (error) {
          showToast(error.message, true);
        }
        renderServicesPage(true);
        updateOverviewStartButton(true);
      });
    });
    listEl.querySelectorAll("[data-service-select]").forEach((el) => {
      el.addEventListener("click", () => selectTerminal(el.dataset.serviceSelect));
    });
  }
  if (terminalId) {
    const svc = services.find((s) => s.id === terminalId);
    if (!svc) {
      stopTerminalPoll();
      terminalId = "";
      terminalCursor = 0;
      $("terminal-title").textContent = t("services.pick");
      $("terminal-body").innerHTML = '<div class="empty-state">' + t("services.removed") + '</div>';
    } else {
      $("terminal-title").textContent = `${svc.name} · ${t(svc.status === "running" ? "state.running" : "state.stopped")}`;
    }
  }
}

function terminalLineClass(line) {
  if (/\[(?:Error|ERROR|FATAL)\]|Traceback \(most recent call last\)/.test(line)) return "error";
  if (/\[Output\]|\[Agent\]/.test(line)) return "output";
  if (/LLM Running/.test(line)) return "llm";
  if (/\[Cache\]/.test(line)) return "cache";
  if (/\[Debug\]/.test(line)) return "debug";
  return "";
}

function appendTerminalLines(lines) {
  const body = $("terminal-body");
  if (!body) return;
  if (body.firstElementChild && body.firstElementChild.classList.contains("empty-state")) body.innerHTML = "";
  const follow = $("terminal-follow")?.classList.contains("active");
  const frag = document.createDocumentFragment();
  lines.forEach((line) => {
    const div = document.createElement("div");
    div.className = `t-line ${terminalLineClass(line)}`;
    div.textContent = line;
    frag.appendChild(div);
  });
  body.appendChild(frag);
  if (!terminalPaused) {
    while (body.childElementCount > 4000) body.removeChild(body.firstChild); // 只保留最近 4000 行
    if (follow) body.scrollTop = body.scrollHeight;
  }
}

function stopTerminalPoll() {
  if (terminalTimer) { clearInterval(terminalTimer); terminalTimer = null; }
}

function startTerminalPoll() {
  stopTerminalPoll();
  const tick = async () => {
    if (document.hidden) return; // 后台标签不轮询，减少连接占用
    if (!terminalId) return;
      if (terminalPaused) return; // 有选区时暂停轮询，保证复制稳定
    try {
      const res = await fetchJson(`/api/services/logs?id=${encodeURIComponent(terminalId)}&cursor=${terminalCursor}`);
      terminalLastError = "";
      if (typeof res.cursor === "number") terminalCursor = res.cursor;
      if (res.lines && res.lines.length) appendTerminalLines(res.lines);
    } catch (error) {
      if (error.message !== terminalLastError) {
        terminalLastError = error.message;
        appendTerminalLines([`[terminal] ${error.message}`]);
      }
    }
  };
  tick();
  terminalTimer = setInterval(tick, 2000);
}

function selectTerminal(sid) {
  stopTerminalPoll();
  terminalId = sid || "";
  terminalCursor = 0;
  terminalLastError = "";
  const body = $("terminal-body");
  if (body) body.innerHTML = `<div class="empty-state">${t("services.connecting", esc(sid))}</div>`;
  const follow = $("terminal-follow");
  if (follow) follow.classList.add("active");
  if (sid) startTerminalPoll();
}

function toggleTerminalFollow() {
  const btn = $("terminal-follow");
  if (!btn) return;
  btn.classList.toggle("active");
  if (btn.classList.contains("active")) {
    const body = $("terminal-body");
    if (body) body.scrollTop = body.scrollHeight;
  }
}

// ---- Worker 页：worker 输出监视器（最新 worker 模型输出流 + run 进度） ----
function startWorkerMonitorPoll() {
  stopWorkerMonitorPoll();
  const tick = async () => {
    if (document.hidden) return; // 后台标签不轮询，减少连接占用
    try {
      const res = await fetchJson(`/api/workers/monitor?cursor=${workerMonitorCursor}&file=${encodeURIComponent(workerMonitorFileId)}&dir=${encodeURIComponent(workerMonitorWorkerDir)}&id=${encodeURIComponent(workerMonitorWorkerId)}`);
      workerMonitorLastError = "";
      if (res.fileId && res.fileId !== workerMonitorFileId) {
        workerMonitorFileId = res.fileId;
        appendWorkerMonitorLines([`${t("monitor.streamHead", res.label || "worker")}`]);
      }
      if (typeof res.cursor === "number") workerMonitorCursor = res.cursor;
      if (res.lines && res.lines.length) appendWorkerMonitorLines(res.lines);
      if (res.progress && res.progress.key !== workerMonitorProgressKey) {
        workerMonitorProgressKey = res.progress.key;
        const summary = String(res.progress.summary || "").replace(/\s+/g, " ").slice(0, 120);
        appendWorkerMonitorLines([`[run] ${res.progress.label || ""} · turn ${res.progress.turn}: ${summary}`]);
      }
    } catch (error) {
      if (error.message !== workerMonitorLastError) {
        workerMonitorLastError = error.message;
        appendWorkerMonitorLines([`[monitor] ${error.message}`]);
      }
    }
  };
  tick();
  workerMonitorTimer = setInterval(tick, 2000);
}

function stopWorkerMonitorPoll() {
  if (workerMonitorTimer) { clearInterval(workerMonitorTimer); workerMonitorTimer = null; }
}

function appendWorkerMonitorLines(lines) {
  const body = $("worker-monitor-body");
  if (!body) return;
  if (body.firstElementChild && body.firstElementChild.classList.contains("empty-state")) body.innerHTML = "";
  const follow = $("worker-monitor-follow")?.classList.contains("active");
  const frag = document.createDocumentFragment();
  lines.forEach((line) => {
    const div = document.createElement("div");
    const cls = /^──|^\[run\]/.test(line) ? "sep" : terminalLineClass(line);
    div.className = `t-line ${cls}`;
    div.textContent = line;
    frag.appendChild(div);
  });
  body.appendChild(frag);
  while (body.childElementCount > 2000) body.removeChild(body.firstChild); // 只保留最近 2000 行
  if (follow) body.scrollTop = body.scrollHeight;
}

function resetWorkerMonitorState() {
  workerMonitorCursor = 0;
  workerMonitorFileId = "";
  workerMonitorProgressKey = "";
}

function selectWorkerMonitor(dir, archive, id, label) {
  stopWorkerMonitorPoll();
  workerMonitorWorkerDir = dir || "";
  workerMonitorWorkerId = id || "";
  resetWorkerMonitorState();
  const body = $("worker-monitor-body");
  if (body) body.innerHTML = "";
  appendWorkerMonitorLines([`${t("monitor.streamHead", label || "worker")}`]);
  const allBtn = $("worker-monitor-all");
  if (allBtn) allBtn.hidden = false;
  const title = $("worker-monitor-title");
  if (title) title.textContent = `${t("monitor.titleShort", label || "worker")}`;
  if (document.querySelector('[data-page-view="workers"]')?.classList.contains("active")) startWorkerMonitorPoll();
}

function clearWorkerMonitorFilter() {
  stopWorkerMonitorPoll();
  workerMonitorWorkerDir = "";
  workerMonitorWorkerId = "";
  resetWorkerMonitorState();
  const body = $("worker-monitor-body");
  if (body) body.innerHTML = "";
  appendWorkerMonitorLines([t("monitor.all")]);
  const allBtn = $("worker-monitor-all");
  if (allBtn) allBtn.hidden = true;
  const title = $("worker-monitor-title");
  if (title) title.textContent = t("monitor.title");
  if (document.querySelector('[data-page-view="workers"]')?.classList.contains("active")) startWorkerMonitorPoll();
}

async function updateOverviewStartButton(force = false) {
  const btn = $("overview-start");
  if (!btn) return;
  const data = await loadServices(force);
  const main = (data.services || []).find((s) => s.id === "main");
  if (!main) {
    btn.textContent = t("state.start");
    btn.classList.remove("running");
    btn.disabled = true;
    return;
  }
  const running = main.status === "running";
  btn.textContent = running ? t("state.startHint") : t("state.start");
  btn.classList.toggle("running", running);
  btn.disabled = false;
}

async function startG4W() {
  const btn = $("overview-start");
  if (!btn || btn.disabled) return;
  if (btn.classList.contains("running")) { location.hash = "services"; routeTo("services"); return; }
  btn.disabled = true;
  btn.textContent = t("state.starting");
  try {
    const res = await fetchJson(`/api/services?action=start&id=main`);
    showToast(res.message || `${t("toast.started")}${t("common.pidInline", res.pid || "?")}`);
    // 与 start_G4W_ga.bat 行为一致：主服务拉起后附带 model monitor（失败不影响主服务）
    try { await fetchJson(`/api/services?action=start&id=monitor`); } catch (error) { /* 忽略 */ }
  } catch (error) {
    showToast(error.message, true);
    btn.textContent = t("state.start");
  }
  updateOverviewStartButton(true);
}

async function loadMemoryDetail(memoryId) {
  if (!memoryId) return;
  currentMemoryId = memoryId;
  $("memory-page-body").innerHTML = '<div class="empty-state glass-card">' + t("memory.loading") + '</div>';
  try {
    renderMemoryDetail(await fetchJson(`/api/memory?id=${encodeURIComponent(memoryId)}`));
  } catch (error) {
    $("memory-page-body").innerHTML = `<div class="empty-state glass-card">${t("memory.readFail", esc(error.message))}</div>`;
  }
}

// ---------- L4 长期记忆整理状态（/api/memory 的 l4 字段）----------
// 后端 memory/maintenance.py 尚未提供 status/trigger 时 l4 缺失/为空 → 整块隐藏，
// 绝不显示 0 或 undefined。
function l4AgoLabel(seconds) {
  if (seconds < 60) return "刚刚";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + " 分钟前";
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + " 小时前";
  return Math.floor(hours / 24) + " 天前";
}

function l4AgoText(l4) {
  const seconds = Number(l4.lastRunAgoSeconds);
  if (l4.lastRunAgoSeconds != null && Number.isFinite(seconds) && seconds >= 0) return l4AgoLabel(seconds);
  return String(l4.lastRunAt || "").trim();
}

function renderL4StatusCard(l4, memoryId) {
  if (!l4 || typeof l4 !== "object") return "";
  const known = ["hasMarkers", "bootstrapped", "lastRunAt", "pendingMessages", "running"];
  if (!known.some((key) => key in l4)) return "";
  const running = l4.running === true;
  const pending = Number(l4.pendingMessages);
  const pendingText = Number.isFinite(pending) ? pending + " 条" : "—";
  const threshold = l4.threshold && typeof l4.threshold === "object" ? l4.threshold : {};
  const minMessages = Number.isFinite(Number(threshold.minMessages)) ? Number(threshold.minMessages) : 15;
  const idleDays = Number.isFinite(Number(threshold.idleDays)) ? Number(threshold.idleDays) : 3;
  const ago = l4AgoText(l4);
  return `<article class="context-card glass-card l4-card"><div class="eyebrow">L4 MAINTENANCE</div><h2>记忆整理</h2>
        <div class="context-row"><span>上次整理</span><strong>${ago ? esc(ago) : "暂无记录"}</strong></div>
        <div class="context-row"><span>待处理</span><strong>${esc(pendingText)}</strong></div>
        <div class="context-row"><span>下次触发</span><strong>≥${minMessages} 条新消息 或 ≥${idleDays} 天</strong></div>
        <button class="l4-compact-btn" id="l4-compact-btn" data-memory-compact="${esc(memoryId)}"${running ? " disabled" : ""}>${running ? "整理中…" : "立即整理"}</button>
      </article>`;
}

async function compactMemory(memoryId) {
  const button = $("l4-compact-btn");
  if (!button || button.disabled) return;
  button.disabled = true;
  button.textContent = "整理中…";
  try {
    const data = await fetchJson("/api/memory/compact", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ memoryId }),
    });
    showToast(data.started
      ? `已开始整理（${data.mode || "deep"}）`
      : (data.reason || "未开始整理"));
    loadMemoryDetail(memoryId);
  } catch (error) {
    showToast(error.message || "触发整理失败", true);
    if (document.body.contains(button)) {
      button.disabled = false;
      button.textContent = "立即整理";
    }
  }
}

function renderMemoryDetail(data) {
  currentMemorySections = data.sections || [];
  currentMemoryData = data;
  const sections = currentMemorySections.map((section) => `<button class="memory-section-row" data-memory-section="${esc(section.key)}"><span>${esc(section.label)}</span><span class="count">${t("memory.sectionRow", section.count)}</span></button>`).join("");
  // 版本历史:默认只显示最新 12 条,其余折叠展开
  const allBackups = data.backups || [];
  const backupItem = (item) => `<div class="artifact"><div><strong>${esc(item.name)}</strong><small>${esc(item.updatedAt)} · ${formatBytes(item.bytes)}</small></div><span class="muted-tag">${t("memory.snapshotTag")}</span></div>`;
  const backupsHtml = allBackups.length
    ? (backupsExpanded ? allBackups : allBackups.slice(0, 12)).map(backupItem).join("")
      + (allBackups.length > 12 ? `<button class="backup-expand" onclick="expandBackups(this)">${backupsExpanded ? t("btn.collapse") : `${t("memory.expandSnapshots", allBackups.length - 12)}`} ${backupsExpanded ? "▴" : "▾"}</button>` : "")
    : '<div class="empty-state">' + t("memory.noVersions") + '</div>';
  const total = (data.sections || []).reduce((sum, section) => sum + (section.count || 0), 0);
  const identity = data.identity ? esc(data.identity) : t("memory.wechatUser");
  const l4Card = renderL4StatusCard(data.l4, data.id);
  const html = `<div class="memory-main">
      <article class="memory-profile-card glass-card">
        <div class="memory-person"><span class="memory-avatar">${esc((data.name || t("memory.tagRecord")).slice(0, 1))}</span><div><h2>${esc(data.name)}</h2><small>${identity}</small></div></div>
        <div class="memory-profile-tags">${data.botName ? `<span class="muted-tag">${t("memory.assistantTag", esc(data.botName))}</span>` : ""}<span class="muted-tag">${t("memory.updatedTag", esc(data.updatedAt))}</span><span class="muted-tag">${formatBytes(data.activeBytes)}</span><span class="page-badge">${t("memory.totalTag", total)}</span></div>
      </article>
      <article class="glass-card memory-block"><div class="section-heading"><div><div class="eyebrow">USER PROFILE</div><h2>${t("memory.secProfile")}</h2></div><span class="muted-tag">${data.userProfilePrev ? t("memory.diffHintThemed") : t("memory.diffHintStable")}</span></div><div class="markdown user-profile">${renderProfileDiff(data.userProfile, data.userProfilePrev)}</div></article>
      <article class="glass-card memory-block"><div class="section-heading"><div><div class="eyebrow">MEMORY BRIEF</div><h2>${t("memory.brief")}</h2></div></div><div class="markdown memory-brief">${renderMarkdown(data.brief || t("memory.noBrief"))}</div></article>
      <article class="glass-card memory-block"><div class="section-heading"><div><div class="eyebrow">PROFILE TIMELINE</div><h2>${t("memory.profileTimeline")}</h2></div><span class="muted-tag">${t("memory.profileTimelineHint")}</span></div><div id="profile-timeline" class="profile-timeline"><div class="empty-state">${t("memory.loadingSnapshot")}</div></div></article>
      <article class="glass-card memory-block"><div class="section-heading"><div><div class="eyebrow">STRUCTURED MEMORY</div><h2>${t("memory.structured")}</h2></div><span class="muted-tag">${t("memory.expandCategoryHint")}</span></div>${sections || '<div class="empty-state">' + t("memory.noStructured") + '</div>'}</article>
    </div>
    <aside class="memory-side">
      ${l4Card}
      <article class="context-card glass-card"><div class="eyebrow">MEMORY STATUS</div><h2>${t("memory.statusTitle")}</h2><div class="context-row"><span>${t("memory.ctxProfile")}</span><strong>${memoryProfiles.length}</strong></div><div class="context-row"><span>${t("memory.ctxVersions")}</span><strong>${(data.backups || []).length}</strong></div><div class="context-row"><span>${t("memory.ctxData")}</span><strong>${formatBytes(data.activeBytes)}</strong></div><div class="context-row"><span>${t("memory.ctxUpdated")}</span><strong>${esc(data.updatedAt)}</strong></div><div class="context-row"><span>${t("memory.ctxSource")}</span><code>${esc(data.source)}</code></div></article>
      <section class="glass-card memory-block"><div class="section-heading"><div><div class="eyebrow">VERSION HISTORY</div><h2>${t("memory.versionHistory")}</h2></div></div>${backupsHtml}</section>
    </aside>`;
  $("memory-page-body").innerHTML = html;
  loadMemoryTimeline(data.id);
}

function loadMemoryTimeline(memoryId) {
  const box = $("profile-timeline");
  if (!box) return;
  fetchJson(`/api/memory/timeline?id=${encodeURIComponent(memoryId)}`).then((data) => {
    timelinePoints = data.points || []; // 后端已按新 → 旧排序
    renderProfileTimeline(box, memoryId);
  }).catch((error) => {
    box.innerHTML = `<div class="empty-state">${t("timeline.loadFail", esc(error.message))}</div>`;
  });
}

function renderProfileTimeline(box, memoryId) {
  const points = timelinePoints;
  if (!points.length) {
    box.innerHTML = '<div class="empty-state">' + t("memory.noSnapshots") + '</div>';
    return;
  }
  const first = points[0], last = points[points.length - 1];
  box.innerHTML = `<div class="timeline-range"><span>${t("memory.rangeLatest", esc(first.updatedAt.split(" ")[0]))}</span><span>${t("memory.snapshotCount", points.length)}</span><span>${t("memory.rangeEarliest", esc(last.updatedAt.split(" ")[0]))}</span></div><div class="timeline-track">${points.map((point) => {
    const profileCount = point.counts && point.counts.user_profile != null ? point.counts.user_profile : "-";
    return `<button class="tl-node" onclick="openBackupSnapshot('${esc(memoryId)}','${esc(point.name)}')" title="${esc(point.updatedAt)} · ${t("memory.profileKeyCount", profileCount)} · ${formatBytes(point.bytes)}"><span class="tl-dot"></span><span class="tl-date">${esc(point.updatedAt.split(" ")[0])}</span><span class="tl-meta">${esc(point.updatedAt.split(" ")[1] || "")}</span></button>`;
  }).join("")}</div>`;
}

function openBackupSnapshot(memoryId, name) {
  openDrawer(t("memory.loadingSnapshot"), "PROFILE SNAPSHOT", '<div class="empty-state">' + t("state.loading") + '</div>');
  const index = timelinePoints.findIndex((point) => point.name === name);
  const prevName = index > 0 ? timelinePoints[index - 1].name : null;
  Promise.all([
    fetchJson(`/api/memory/backup?id=${encodeURIComponent(memoryId)}&name=${encodeURIComponent(name)}`),
    prevName ? fetchJson(`/api/memory/backup?id=${encodeURIComponent(memoryId)}&name=${encodeURIComponent(prevName)}`) : Promise.resolve(null),
  ]).then(([current, prev]) => {
    openDrawer(`${t("memory.snapshotTitle", current.updatedAt)}`, "PROFILE SNAPSHOT", renderBackupSnapshot(current, prev));
  }).catch((error) => {
    openDrawer(t("memory.snapshotReadFail"), "ERROR", `<div class="empty-state">${esc(error.message)}</div>`);
  });
}

const PROFILE_KEY_LABELS = { user_profile: t("memory.secProfile"), ongoing_projects: t("memory.secProjects"), agent_capabilities_learned: t("memory.secSkills"), memory_lessons: t("memory.secExperience"), user_facts: t("memory.secFacts"), _meta: t("memory.secMeta") };

function diffValues(cur, prev) {
  if (Array.isArray(cur) || Array.isArray(prev)) {
    const curArr = Array.isArray(cur) ? cur : [];
    const prevArr = Array.isArray(prev) ? prev : [];
    const keyOf = (value) => JSON.stringify(value);
    const prevKeys = new Set(prevArr.map(keyOf));
    const curKeys = new Set(curArr.map(keyOf));
    return { added: curArr.filter((value) => !prevKeys.has(keyOf(value))), removed: prevArr.filter((value) => !curKeys.has(keyOf(value))), changed: [] };
  }
  if (cur && typeof cur === "object" && prev && typeof prev === "object") {
    const added = [], removed = [], changed = [];
    for (const key of Object.keys(prev)) if (!(key in cur)) removed.push([key, prev[key]]);
    for (const key of Object.keys(cur)) {
      if (!(key in prev)) added.push([key, cur[key]]);
      else if (JSON.stringify(cur[key]) !== JSON.stringify(prev[key])) changed.push({ key, before: prev[key], after: cur[key] });
    }
    return { added, removed, changed };
  }
  if (JSON.stringify(cur) !== JSON.stringify(prev)) return { added: [cur], removed: [prev], changed: [] };
  return { added: [], removed: [], changed: [] };
}

function diffBadges(diff) {
  const badges = [];
  if (diff.added.length) badges.push(`<span class="diff-badge add">+${diff.added.length}</span>`);
  if (diff.removed.length) badges.push(`<span class="diff-badge rem">−${diff.removed.length}</span>`);
  if (diff.changed.length) badges.push(`<span class="diff-badge chg">~${diff.changed.length}</span>`);
  return badges.length ? `<span class="diff-badges">${badges.join("")}</span>` : "";
}

function renderSnapEntry(value) {
  if (Array.isArray(value)) return renderSnapList(value, null);
  if (value && typeof value === "object") return memoryRecord(value);
  return `<p>${esc(value ?? "")}</p>`;
}

function renderSnapList(curArr, prevArr) {
  const diff = diffValues(curArr, prevArr);
  const addedKeys = new Set(diff.added.map((value) => JSON.stringify(value)));
  const removedKeys = new Set(diff.removed.map((value) => JSON.stringify(value)));
  const parts = [];
  if (diff.added.length) parts.push(`<div class="snap-note add">${t("memory.snapAdded", diff.added.length)}</div>`);
  if (diff.removed.length) parts.push(`<div class="snap-note rem">${t("memory.snapRemoved", diff.removed.length)}</div>`);
  for (const item of curArr) {
    const cls = addedKeys.has(JSON.stringify(item)) ? "snap-added" : "";
    parts.push(`<div class="snap-item ${cls}">${renderSnapEntry(item)}</div>`);
  }
  if (prevArr) {
    for (const item of prevArr) {
      if (!addedKeys.has(JSON.stringify(item)) && !curArr.some((cur) => JSON.stringify(cur) === JSON.stringify(item))) {
        parts.push(`<div class="snap-item snap-removed">${renderSnapEntry(item)}</div>`);
      }
    }
  }
  return parts.join("");
}

function renderBackupSnapshot(current, prev) {
  const curActive = current.active || {};
  const prevActive = prev && prev.active ? prev.active : null;
  const keys = [...new Set([...(prevActive ? Object.keys(prevActive) : []), ...Object.keys(curActive)])];
  const blocks = keys.map((key) => {
    const label = PROFILE_KEY_LABELS[key] || key;
    const curValue = curActive[key];
    const prevValue = prevActive ? prevActive[key] : null;
    const diff = prevActive ? diffValues(curValue, prevValue) : null;
    let body;
    if (Array.isArray(curValue)) {
      body = renderSnapList(curValue, prevValue);
    } else if (curValue && typeof curValue === "object") {
      const rows = Object.entries(curValue).map(([childKey, childValue]) => {
        const childDiff = prevActive && prevValue && typeof prevValue === "object" ? diffValues(childValue, prevValue[childKey]) : null;
        const badges = childDiff ? diffBadges(childDiff) : "";
        const childBody = Array.isArray(childValue) ? renderSnapList(childValue, prevActive && prevValue && Array.isArray(prevValue[childKey]) ? prevValue[childKey] : null) : `<div class="snap-item">${renderSnapEntry(childValue)}</div>`;
        return `<details class="accordion"><summary><span>${esc(childKey.replaceAll("_", " "))}</span>${badges}</summary><div class="accordion-body">${childBody}</div></details>`;
      });
      body = rows.join("");
    } else {
      body = `<div class="snap-item">${esc(curValue ?? "")}</div>`;
    }
    return `<details class="accordion snap-key"><summary><span>${esc(label)}</span>${diff ? diffBadges(diff) : ""}</summary><div class="accordion-body">${body}</div></details>`;
  }).join("");
  const pills = [`<span>${esc(current.updatedAt)}</span>`, `<span>${formatBytes(JSON.stringify(curActive).length)}</span>`, `<span>${t("memory.categoryCount", keys.length)}</span>`];
  if (prev) pills.push(`<span>${t("memory.compareWith", esc(prev.updatedAt.split(" ")[0]))}</span>`);
  const briefHtml = current.brief ? `<article class="snap-brief"><div class="eyebrow">${t("memory.briefAtTheTime")}</div><div class="markdown">${renderMarkdown(current.brief)}</div></article>` : "";
  return `<div class="detail-pills">${pills.join("")}</div>${briefHtml}<div class="snap-keys">${blocks}</div>`;
}

function selectPromptNode(node, button) {
  document.querySelectorAll(".tree-node").forEach((item) => item.classList.remove("active"));
  if (button) button.classList.add("active");
  $("prompt-node-meta").innerHTML = [t("prompt.sourceFile", node.source || "--"), `${node.chars || 0} chars`, `≈ ${node.tokens || 0} tokens`, `SHA ${node.sha || "--"}`, node.updatedAt || "--", node.inModel ? t("prompt.enterModel") : t("prompt.uiOnly")].map((value) => `<span>${esc(value)}</span>`).join("");
  $("prompt-node-content").textContent = node.content || t("prompt.nodeEmpty");
}

function renderPromptTree(data) {
  const nodes = [...(data.components || []), data.thinkingNode].filter(Boolean);
  $("prompt-tree").innerHTML = nodes.map((node, index) => `<div class="tree-group"><button class="tree-node ${index === 0 ? "active" : ""}" data-prompt-node="${index}"><i>${node.kind === "generated" ? "◇" : node.kind === "code" ? "⌘" : "▧"}</i><span>${esc(node.title)}</span><small>${node.chars || 0}</small></button>${(node.children || []).length ? `<div class="tree-children">${node.children.map((child, childIndex) => `<button class="tree-node" data-prompt-node="${index}" data-prompt-child="${childIndex}"><i>└</i><span>${esc(child.title)}</span><small>${child.chars || 0}</small></button>`).join("")}</div>` : ""}</div>`).join("");
  if (nodes[0]) selectPromptNode(nodes[0], $("prompt-tree").querySelector(".tree-node"));
}

async function loadPrompt() {
  $("prompt-tree").innerHTML = '<div class="empty-state">' + t("prompt.loadingLast") + '</div>';
  try {
    promptData = await fetchJson("/api/prompt");
    $("prompt-model").textContent = promptData.model || "--";
    $("prompt-time").textContent = promptData.generatedAt || "--";
    $("prompt-chars").textContent = String((promptData.final || "").length);
    $("prompt-source").textContent = promptData.snapshotPath || t("prompt.noInputSnapshot");
    $("prompt-final").textContent = promptData.final || t("prompt.noSnapshot");
    $("prompt-dynamic").textContent = promptData.dynamic || t("prompt.noDynamic");
    renderPromptTree(promptData);
  } catch (error) { $("prompt-tree").innerHTML = `<div class="empty-state">${esc(error.message)}</div>`; }
}

function fillModelSelect(select, models, selectedValue, useIndex = false) {
  const rows = [...models];
  const hasSelected = rows.some((item) => String(useIndex ? item.index : item.model) === String(selectedValue));
  if (selectedValue !== "" && selectedValue != null && !hasSelected) rows.push({ index: "", label: selectedValue, model: selectedValue, current: false });
  select.innerHTML = rows.map((item) => {
    const value = useIndex ? item.index : item.model;
    const label = item.index === "" ? item.label : `[${item.index}] ${item.label}`;
    return `<option value="${esc(value)}">${esc(label)}${item.current && useIndex ? " · " + t("state.current") + "" : ""}</option>`;
  }).join("");
  select.value = String(selectedValue ?? "");
  select.dataset.committedValue = select.value;
  select.disabled = false;
}

function applyModelData(data) {
  const models = data.models || [];
  const current = data.current || models.find((item) => item.current) || {};
  fillModelSelect($("setting-conductor-model"), models, current.index ?? "", true);
  fillModelSelect($("setting-worker-model"), models, data.workerModel ?? dashboardData?.configuration?.workerModel ?? "", false);
  fillModelSelect($("setting-pro-model"), models, data.proModel ?? dashboardData?.configuration?.proModel ?? "", false);
  $("model-control-status").textContent = `${t("model.availableOnline", models.length)}`;
}

async function loadModels(force = false) {
  if (modelsLoading || (!force && $("setting-conductor-model").dataset.loaded === "true")) return;
  modelsLoading = true;
  $("model-control-status").textContent = t("persona.loading");
  try {
    const data = await fetchJson("/api/models");
    applyModelData(data);
    $("setting-conductor-model").dataset.loaded = "true";
  } catch (error) {
    document.querySelectorAll("[data-model-target]").forEach((select) => { select.disabled = true; });
    $("model-control-status").textContent = t("state.mainOffline");
    showToast(error.message, true);
  } finally { modelsLoading = false; }
}

async function switchModel(select) {
  const target = select.dataset.modelTarget;
  const previous = select.dataset.committedValue || "";
  const value = select.value;
  document.querySelectorAll("[data-model-target]").forEach((item) => { item.disabled = true; });
  $("model-control-status").textContent = t("model.switching");
  try {
    const result = await fetchJson("/api/model", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target, value }) });
    showToast(result.message || t("model.switched"));
    // 切换后首页立刻反映新模型：乐观更新本地快照并重绘，再拉服务端真值 + 一次延迟兜底
    const appliedModel = String((result && result.selected && result.selected.model) || value || "").trim();
    if (appliedModel && dashboardData && dashboardData.system) {
      if (target === "conductor") dashboardData.system.model = appliedModel;
      render(dashboardData);
    }
    window.setTimeout(() => { refresh(); }, 800);
    document.querySelectorAll("[data-model-target]").forEach((item) => { item.dataset.loaded = ""; });
    await refresh();
    await loadModels(true);
  } catch (error) {
    select.value = previous;
    showToast(error.message, true);
    $("model-control-status").textContent = t("model.switchFail");
    document.querySelectorAll("[data-model-target]").forEach((item) => { item.disabled = false; });
  }
}

function collectSettingsPayload() {
  return {
    userName: $("setting-user-name").value.trim(), userIdentity: $("setting-user-identity").value.trim(), userGender: $("setting-user-gender").value, botName: $("setting-bot-name").value.trim(),
    turnEnabled: $("setting-turn").checked, workerTurnEnabled: $("setting-worker-turn").checked, maintenanceTurnEnabled: $("setting-maintenance-turn").checked, inputCaptureEnabled: $("setting-input").checked, chunkChars: Number($("setting-chunk").value),
    maintenanceActions: Array.from(document.querySelectorAll("[data-maintenance-action]")).filter((el) => el.checked).map((el) => el.dataset.maintenanceAction),
    workspaceRoot: $("setting-workspace").value.trim(),
    checkinEnabled: $("setting-checkin").checked, checkinMin: Number($("setting-checkin-min").value), checkinMax: Number($("setting-checkin-max").value), vectorEnabled: $("setting-vector").checked, webSearchEnabled: $("setting-web-search").checked,
    uiLocale: uiPrefs.locale, uiTimezone: uiPrefs.timezone
  };
}

// ---------- 界面偏好（语言 / 时区）：/api/dashboard 的 system.ui → localStorage → 默认 ----------
function fillUiPrefControls() {
  const localeSelect = $("setting-ui-locale");
  if (localeSelect && !localeSelect.options.length) {
    const codes = I18N ? I18N.LANGS : ["zh-CN"];
    localeSelect.innerHTML = codes.map((code) => `<option value="${esc(code)}">${esc((I18N && I18N.langLabels[code]) || code)}</option>`).join("");
  }
  const zoneSelect = $("setting-ui-timezone");
  if (zoneSelect && !zoneSelect.options.length) {
    const options = I18N ? I18N.timezoneOptions() : [{ value: "", label: t("env.tz.followSystem") }];
    zoneSelect.innerHTML = options.map((item) => `<option value="${esc(item.value)}">${esc(item.label)}</option>`).join("");
  }
  if (localeSelect) localeSelect.value = uiPrefs.locale;
  if (zoneSelect) zoneSelect.value = uiPrefs.timezone;
}

function rerenderLocalizedViews() {
  try {
    if (dashboardData) render(dashboardData);
    if (timelineData) renderTimeline();
    if (diaryData) renderDiaryIndex();
    routeTo(currentPage());
  } catch (error) { console.warn("G4W re-render after locale change failed", error); }
}

function applyUiPrefs(locale, timezone, options = {}) {
  const { persist = true, rerender = true } = options;
  uiPrefs.locale = I18N && I18N.LANGS.includes(locale) ? locale : "zh-CN";
  uiPrefs.timezone = I18N ? I18N.resolveTimezone(timezone || "") : "";
  if (I18N) { I18N.setLang(uiPrefs.locale); I18N.setTimezone(uiPrefs.timezone); }
  if (persist) {
    localStorage.setItem("g4w-ui-locale", uiPrefs.locale);
    localStorage.setItem("g4w-ui-timezone", uiPrefs.timezone);
  }
  fillUiPrefControls();
  if (rerender) rerenderLocalizedViews();
}

function hydrateUiPrefs(data) {
  if (uiPrefsHydrated) return;
  const ui = (data && data.system && data.system.ui) || {};
  const storedLocale = localStorage.getItem("g4w-ui-locale") || "";
  const storedZone = localStorage.getItem("g4w-ui-timezone") || "";
  // 旧后端没有 system.ui 且本地也没有偏好时先不锁定，等真正带 ui 的响应再水合
  if (typeof ui.locale !== "string" && typeof ui.timezone !== "string" && !storedLocale) return;
  uiPrefsHydrated = true;
  const locale = [ui.locale, storedLocale].find((value) => I18N && I18N.LANGS.includes(value)) || uiPrefs.locale;
  const timezone = typeof ui.timezone === "string" ? ui.timezone : storedZone;
  applyUiPrefs(locale, timezone, { persist: false, rerender: false });
}

async function persistUiPrefs(previous) {
  const message = $("ui-pref-message");
  try {
    const result = await fetchJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collectSettingsPayload()) });
    settingsDirty = false;
    if (message) { message.hidden = true; message.textContent = ""; }
    if (result && result.message) showToast(result.message);
    await refresh();
  } catch (error) {
    applyUiPrefs(previous.locale, previous.timezone, { persist: true, rerender: true });
    if (message) { message.hidden = false; message.textContent = error.message || t("env.tz.invalid"); }
    showToast(error.message || t("env.tz.invalid"), true);
  }
}

function setupUiPrefControls() {
  fillUiPrefControls();
  const localeSelect = $("setting-ui-locale");
  const zoneSelect = $("setting-ui-timezone");
  if (localeSelect) localeSelect.addEventListener("change", async () => {
    const previous = { locale: uiPrefs.locale, timezone: uiPrefs.timezone };
    applyUiPrefs(localeSelect.value, uiPrefs.timezone, { persist: true, rerender: true });
    await persistUiPrefs(previous);
  });
  if (zoneSelect) zoneSelect.addEventListener("change", async () => {
    const previous = { locale: uiPrefs.locale, timezone: uiPrefs.timezone };
    applyUiPrefs(uiPrefs.locale, zoneSelect.value, { persist: true, rerender: true });
    await persistUiPrefs(previous);
  });
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = collectSettingsPayload();
  const button = $("settings-save");
  button.disabled = true; button.textContent = t("env.saving");
  try {
    const result = await fetchJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    settingsDirty = false;
    $("settings-message").textContent = result.message;
    showToast(result.message);
    await refresh();
  } catch (error) { $("settings-message").textContent = error.message; showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = t("env.save"); }
}

$("refresh-button").addEventListener("click", refreshNowFixed);
$("theme-button").addEventListener("click", cycleTheme);
$("menu-button").addEventListener("click", () => setSidebarOpen(!$("sidebar").classList.contains("open")));
$("sidebar-close").addEventListener("click", () => setSidebarOpen(false));
$("sidebar-scrim").addEventListener("click", () => setSidebarOpen(false));
$("theme-mode-select").addEventListener("change", (event) => {
  localStorage.setItem("g4w-theme", event.target.value);
  applyAppearance(event.target.value, document.documentElement.dataset.palette || "wechat");
});
$("memory-profile-select").addEventListener("change", (event) => {
  localStorage.setItem("g4w-memory-profile", event.target.value);
  loadMemoryDetail(event.target.value);
});
$("timeline-date").addEventListener("change", (event) => { timelineDate = event.target.value; renderTimeline(); });

// ---------- 人设（persona presets）：分节编辑 / 保存 / 注入 ----------
let personaData = null;
let personaDraft = null;      // {id, name, sections}
let personaEditingId = "";

function personaMessage(text, isError = false) {
  const node = $("persona-message");
  if (node) {
    node.textContent = text || "";
    node.style.color = isError ? "var(--danger, #e5484d)" : "";
  }
  if (text) showToast(text, isError);
}

function personaClone(value) { return JSON.parse(JSON.stringify(value || [])); }

function personaRenderVars(data) {
  const box = $("persona-vars");
  if (!box) return;
  const variables = (data && data.variables) || [];
  box.innerHTML = variables.map((item) => `
    <button type="button" class="persona-var" data-copy="${esc(item.token)}" title="${t("persona.copyBacktick")}">
      <code>${esc("`" + item.token + "`")}</code><small>${esc(item.label)} · ${esc(item.desc)}</small>
    </button>`).join("") || '<span class="empty-state">' + t("persona.noVars") + '</span>';
  box.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const text = "`" + button.dataset.copy + "`";
      try {
        await navigator.clipboard.writeText(text);
        personaMessage(`${t("toast.copied", text)}`);
      } catch (error) {
        personaMessage(`${t("toast.copyFail", text)}`, true);
      }
    });
  });
}

function personaRenderList() {
  const box = $("persona-list");
  if (!box || !personaData) return;
  const rows = personaData.presets || [];
  box.innerHTML = rows.map((item) => `
    <button type="button" class="persona-item ${item.id === personaEditingId ? "active" : ""}" data-id="${esc(item.id)}">
      <strong>${esc(item.name)}${item.id === personaData.activeId ? ' <span class="tag">' + t("persona.activeTag") + '</span>' : ""}</strong>
      <small>${item.builtin ? t("persona.builtin") : t("persona.custom")} · ${t("persona.charsCount", item.chars)}${item.sections ? ` · ${t("persona.sectionCountShort", item.sections.length)}` : ""}</small>
    </button>`).join("") || '<div class="empty-state">' + t("persona.noPresets") + '</div>';
  box.querySelectorAll("[data-id]").forEach((button) => {
    button.addEventListener("click", () => personaSelect(button.dataset.id));
  });
  const meta = $("persona-meta");
  if (meta) {
    const backups = (personaData.backups || []).filter((item) => String(item.name).includes(personaEditingId));
    meta.innerHTML = [
      `${t("persona.runtimeFileLabel")}<code>${esc(personaData.runtimeFile || "")}</code>${t("persona.bytesWrap", personaData.runtimeBytes || 0)}`,
      `${t("persona.backupHint", personaData.backupKeep || 3, backups.length)}`,
      personaData.activeRegistered === false && personaData.activeId ? t("persona.activeMissing") : "",
    ].filter(Boolean).join("<br>");
  }
}

function personaSelect(id) {
  if (!personaData) return;
  const item = (personaData.presets || []).find((row) => row.id === id);
  if (!item) return;
  personaEditingId = id;
  personaDraft = { id: item.id, name: item.name, sections: personaClone(item.sections) };
  personaRenderList();
  personaRenderEditor();
}

function personaRenderEditor() {
  const box = $("persona-sections");
  const title = $("persona-editor-title");
  if (!box || !personaDraft) return;
  if (title) title.textContent = `${t("persona.editTitle", personaDraft.name)}`;
  const sections = personaDraft.sections || [];
  box.innerHTML = sections.map((section, index) => `
    <div class="persona-section" data-section="${index}">
      <div class="persona-section-head">
        <span class="persona-tag">${t("persona.sectionLabel", index + 1)}</span>
        <button type="button" class="ghost-button" data-act="up">↑</button>
        <button type="button" class="ghost-button" data-act="down">↓</button>
        <button type="button" class="ghost-button" data-act="del-section">${t("persona.delete")}</button>
      </div>
      <label>${t("persona.titleLabel")}<input data-field="title" value="${esc(section.title || "")}" placeholder="${t("persona.required")}" /></label>
      <label>${t("persona.introLabel")}<textarea data-field="intro" rows="2" placeholder="${t("persona.introPlaceholder")}">${esc(section.intro || "")}</textarea></label>
      ${(section.children || []).map((child, childIndex) => `
        <div class="persona-child" data-child="${childIndex}">
          <div class="persona-child-head">
            <span class="persona-tag">${t("persona.childLabel", (index + 1) + "." + (childIndex + 1))}</span>
            <button type="button" class="ghost-button" data-act="child-up">↑</button>
            <button type="button" class="ghost-button" data-act="child-down">↓</button>
            <button type="button" class="ghost-button" data-act="del-child">${t("persona.delete")}</button>
          </div>
          <label>${t("persona.childTitleLabel")}<input data-field="child-title" value="${esc(child.title || "")}" placeholder="${t("persona.required")}" /></label>
          <label>${t("persona.bodyLabel")}<textarea data-field="child-body" rows="5">${esc(child.body || "")}</textarea></label>
        </div>`).join("")}
      <div class="persona-add-row"><button type="button" class="ghost-button" data-act="add-child">+ ${t("persona.addChild")}</button></div>
    </div>`).join("") || '<div class="empty-state">' + t("persona.noSections") + '</div>';
  personaUpdatePreview();
}

function personaCollect() {
  const box = $("persona-sections");
  if (!box || !personaDraft) return;
  const sections = [];
  box.querySelectorAll("[data-section]").forEach((node) => {
    const section = {
      title: node.querySelector('[data-field="title"]').value.trim(),
      intro: node.querySelector('[data-field="intro"]').value,
      children: [],
    };
    node.querySelectorAll("[data-child]").forEach((childNode) => {
      section.children.push({
        title: childNode.querySelector('[data-field="child-title"]').value.trim(),
        body: childNode.querySelector('[data-field="child-body"]').value,
      });
    });
    sections.push(section);
  });
  personaDraft.sections = sections;
}

function personaMarkdown() {
  if (!personaDraft) return "";
  return (personaDraft.sections || []).map((section) => {
    const parts = [`## ${section.title || t("persona.untitledSection")}`];
    if (String(section.intro || "").trim()) parts.push(String(section.intro).trim());
    (section.children || []).forEach((child) => {
      parts.push(`### ${child.title || t("persona.untitledSub")}`);
      if (String(child.body || "").trim()) parts.push(String(child.body).trim());
    });
    return parts.join("\n\n");
  }).join("\n\n---\n\n") + "\n";
}

function personaUpdatePreview() {
  const node = $("persona-preview");
  if (node && personaDraft) node.textContent = personaMarkdown();
}

async function loadPersona() {
  const badge = $("persona-badge");
  if (badge) badge.textContent = "LOADING";
  try {
    personaData = await fetchJson("/api/persona");
    personaRenderVars(personaData);
    const rows = personaData.presets || [];
    if (!personaEditingId || !rows.some((row) => row.id === personaEditingId)) {
      const preferred = rows.find((row) => row.id === personaData.activeId) || rows[0];
      personaEditingId = preferred ? preferred.id : "";
    }
    personaRenderList();
    if (personaEditingId) personaSelect(personaEditingId);
    else $("persona-sections").innerHTML = '<div class="empty-state">' + t("persona.noPresets") + '</div>';
    if (badge) badge.textContent = `${t("persona.presetCount", rows.length)}`;
  } catch (error) {
    if (badge) badge.textContent = "ERROR";
    const box = $("persona-sections");
    if (box) box.innerHTML = `<div class="empty-state">${esc(error.message)}</div>`;
  }
}

async function personaSend(path, payload, okMessage) {
  personaMessage(t("state.working"));
  try {
    const result = await fetchJson(`/api/persona/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    personaData = result;
    personaRenderVars(personaData);
    personaRenderList();
    personaMessage(okMessage || t("toast.done"));
    return result;
  } catch (error) {
    personaMessage(error.message, true);
    return null;
  }
}

async function personaSave() {
  if (!personaDraft) return;
  personaCollect();
  const result = await personaSend("save", { id: personaDraft.id, name: personaDraft.name, sections: personaDraft.sections }, t("toast.saved"));
  if (!result) return;
  const item = (result.presets || []).find((row) => row.id === personaDraft.id);
  if (item) personaDraft.sections = personaClone(item.sections);
  personaRenderEditor();
  const warnings = (result.preset && result.preset.warnings) || [];
  if (warnings.length) personaMessage(warnings.join(" "), true);
  else if (result.preset && result.preset.runtimeUpdated) personaMessage(t("persona.savedSynced"));
}

async function personaInject() {
  if (!personaDraft) return;
  const button = $("persona-inject");
  if (button) { button.disabled = true; button.textContent = t("persona.injecting"); }
  personaMessage(t("persona.activating"));
  try {
    // 第一步：激活并投递 reread（立刻返回，避免前端 10s 超时）
    const result = await fetchJson("/api/persona/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: personaDraft.id }),
    });
    personaData = result;
    personaRenderList();
    promptData = null;                     // 提示词页的「微信人格」节点是现读文件的 → 下次进入自动刷新
    if (currentPage() === "prompt") loadPrompt();
    personaMessage(result.note || t("persona.activated"));
    if (!result.injectId) return;
    // 第二步：轮询 reread 结果（会跑一次真实模型轮次，可能几十秒）
    if (button) button.textContent = t("persona.rereading");
    for (let attempt = 0; attempt < 80; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      let status = null;
      try {
        status = await fetchJson(`/api/persona/inject-status?id=${encodeURIComponent(result.injectId)}`);
      } catch (error) {
        personaMessage(`${t("persona.injectStatusFail", error.message)}`, true);
        return;
      }
      if (status && status.done) {
        personaMessage([status.note, status.reply].filter(Boolean).join(" "), status.injectOk === false);
        return;
      }
    }
    personaMessage(t("persona.injectSlow"), true);
  } catch (error) {
    personaMessage(error.message, true);
  } finally {
    if (button) { button.disabled = false; button.textContent = t("persona.inject"); }
  }
}

async function personaCreate() {
  const name = window.prompt(t("persona.newPresetName"), t("persona.newPreset"));
  if (!name) return;
  const result = await personaSend("create", { name }, `${t("persona.created", name)}`);
  if (result && result.createdId) personaSelect(result.createdId);
}

async function personaCopy() {
  if (!personaDraft) return;
  const name = window.prompt(t("persona.copyAs"), `${t("persona.copyNameSuffix", personaDraft.name)}`);
  if (!name) return;
  const result = await personaSend("create", { name, sourceId: personaDraft.id }, `${t("persona.copiedAs", name)}`);
  if (result && result.createdId) personaSelect(result.createdId);
}

async function personaRename() {
  if (!personaDraft) return;
  const name = window.prompt(t("persona.newTitle"), personaDraft.name);
  if (!name) return;
  const result = await personaSend("rename", { id: personaDraft.id, name }, t("persona.renamed"));
  if (result) personaSelect(personaDraft.id);
}

async function personaDelete() {
  if (!personaDraft) return;
  if (!window.confirm(t("persona.delete.confirm", personaDraft.name))) return;
  const result = await personaSend("delete", { id: personaDraft.id }, t("toast.deleted"));
  if (result) {
    personaEditingId = "";
    personaDraft = null;
    await loadPersona();
  }
}

async function personaRestore() {
  if (!personaDraft) return;
  if (!window.confirm(t("persona.restore.confirm", personaDraft.name))) return;
  const result = await personaSend("restore", { id: personaDraft.id }, t("persona.restored"));
  if (result) personaSelect(personaDraft.id);
}

async function personaImport() {
  const name = ($("persona-io-name").value || "").trim() || t("persona.import");
  const markdown = $("persona-io-text").value || "";
  if (!markdown.trim()) { personaMessage(t("persona.importEmpty"), true); return; }
  const result = await personaSend("import", { name, markdown }, `${t("persona.imported", name)}`);
  if (!result) return;
  if (result.id) personaSelect(result.id);
  const warnings = result.parseWarnings || [];
  if (warnings.length) personaMessage(warnings.join(" "), true);
}

async function personaExport() {
  if (!personaDraft) return;
  try {
    const result = await fetchJson(`/api/persona/export?id=${encodeURIComponent(personaDraft.id)}`);
    $("persona-io-text").value = result.markdown || "";
    personaMessage(`${t("persona.exported", personaDraft.name)}`);
  } catch (error) {
    personaMessage(error.message, true);
  }
}

async function personaImportRuntime() {
  const name = ($("persona-io-name").value || "").trim() || t("persona.currentRuntime");
  const result = await personaSend("import-runtime", { name }, `${t("persona.importedRuntime", name)}`);
  if (result && result.id) personaSelect(result.id);
}

function personaBindEditor() {
  const box = $("persona-sections");
  if (!box) return;
  box.addEventListener("click", (event) => {
    const button = event.target.closest("[data-act]");
    if (!button || !personaDraft) return;
    event.preventDefault();
    const sectionNode = button.closest("[data-section]");
    const childNode = button.closest("[data-child]");
    const index = sectionNode ? Number(sectionNode.dataset.section) : -1;
    const childIndex = childNode ? Number(childNode.dataset.child) : -1;
    personaCollect();
    const sections = personaDraft.sections || [];
    const children = index >= 0 ? (sections[index].children = sections[index].children || []) : [];
    const action = button.dataset.act;
    if (action === "up" && index > 0) [sections[index - 1], sections[index]] = [sections[index], sections[index - 1]];
    else if (action === "down" && index >= 0 && index < sections.length - 1) [sections[index + 1], sections[index]] = [sections[index], sections[index + 1]];
    else if (action === "del-section" && index >= 0) sections.splice(index, 1);
    else if (action === "add-child" && index >= 0) children.push({ title: t("persona.newChild"), body: "" });
    else if (action === "del-child" && childIndex >= 0) children.splice(childIndex, 1);
    else if (action === "child-up" && childIndex > 0) [children[childIndex - 1], children[childIndex]] = [children[childIndex], children[childIndex - 1]];
    else if (action === "child-down" && childIndex >= 0 && childIndex < children.length - 1) [children[childIndex + 1], children[childIndex]] = [children[childIndex], children[childIndex + 1]];
    personaRenderEditor();
  });
  box.addEventListener("input", () => { personaCollect(); personaUpdatePreview(); });
}

$("persona-add-section")?.addEventListener("click", () => {
  if (!personaDraft) return;
  personaCollect();
  personaDraft.sections = personaDraft.sections || [];
  personaDraft.sections.push({ title: t("persona.newParent"), intro: "", children: [] });
  personaRenderEditor();
});
$("persona-save")?.addEventListener("click", personaSave);
$("persona-inject")?.addEventListener("click", personaInject);
$("persona-new")?.addEventListener("click", personaCreate);
$("persona-copy")?.addEventListener("click", personaCopy);
$("persona-rename")?.addEventListener("click", personaRename);
$("persona-delete")?.addEventListener("click", personaDelete);
$("persona-restore")?.addEventListener("click", personaRestore);
$("persona-io-import")?.addEventListener("click", personaImport);
$("persona-io-export")?.addEventListener("click", personaExport);
$("persona-io-runtime")?.addEventListener("click", personaImportRuntime);
personaBindEditor();

// ---------- 供应商配置 + 模型配置（拉取 / 测试 / 添加 / 删除） ----------
let modelConfigData = null;
let modelFetchState = { providerId: "", providerName: "", models: [] };
let modelTestResults = {};

function providerMessage(text, isError = false) {
  const node = $("provider-config-message");
  if (node) {
    node.textContent = text || "";
    node.style.color = isError ? "var(--danger, #e5484d)" : "";
  }
  if (text) showToast(text, isError);
}

function modelMessage(text, isError = false) {
  const node = $("model-config-message");
  if (node) {
    node.textContent = text || "";
    node.style.color = isError ? "var(--danger, #e5484d)" : "";
  }
  if (text) showToast(text, isError);
}

async function loadModelConfig() {
  try {
    modelConfigRender(await fetchJson("/api/model-config"));
  } catch (error) {
    modelMessage(`${t("common.readFailPrefix", error.message)}`, true);
  }
}

async function modelConfigPost(action, payload, announce) {
  try {
    return await fetchJson(`/api/model-config/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
  } catch (error) {
    (announce || modelMessage)(error.message, true);
    return null;
  }
}

function testKey(providerId, model) {
  return `${providerId}::${model}`;
}

function testLine(providerId, model) {
  const info = modelTestResults[testKey(providerId, model)];
  if (!info) return "";
  if (info.ok) {
    const reply = info.reply ? `${t("model.replyQuoted", esc(info.reply))}` : "";
    return `<span class="model-test ok">✓ ${esc(String(info.latencyMs ?? "?"))} ms${reply}</span>`;
  }
  const detail = String(info.error || t("model.testFail"));
  return `<span class="model-test err" title="${esc(detail)}">✗ ${esc(detail)}</span>`;
}

// 添加 / 详情走弹窗：长报错只影响弹窗内部，不再撑破网格（2026-10-15 用户反馈的破版）
let modelModalState = null;

function openModelModal({ providerId, providerName, model, addable }) {
  const box = $("model-modal");
  if (!box) return;
  modelModalState = { providerId, providerName, model, addable: !!addable };
  $("model-modal-title").textContent = model;
  $("model-modal-base").textContent = `${providerName || providerId} · ${providerId}`;
  const add = $("model-modal-add");
  if (add) { add.hidden = !modelModalState.addable; add.disabled = false; add.textContent = t("modal.add"); }
  const res = $("model-modal-result");
  if (res) { res.hidden = true; res.textContent = ""; res.classList.remove("ok", "err"); res.removeAttribute("title"); }
  box.hidden = false;
}

function closeModelModal() {
  const box = $("model-modal");
  if (box) box.hidden = true;
  modelModalState = null;
}

async function modelModalTest() {
  if (!modelModalState) return;
  const { providerId, model } = modelModalState;
  const res = $("model-modal-result");
  if (!res) return;
  res.hidden = false;
  res.classList.remove("ok", "err");
  res.textContent = `${t("model.testingModel", model)}`;
  const result = await modelTestRaw(providerId, model);
  modelTestResults[testKey(providerId, model)] = result;
  if (result.ok) {
    res.classList.add("ok");
    res.textContent = `${t("model.okLatency", result.latencyMs ?? "?")}` + (result.reply ? t("model.replyLine", result.reply) : "");
  } else {
    res.classList.add("err");
    res.textContent = t("common.failLabel") + "\n" + (result.error || t("toast.unknownError"));
  }
  res.title = res.textContent;
}

async function modelModalAdd() {
  if (!modelModalState || !modelModalState.addable) return;
  const { providerId, model } = modelModalState;
  const button = $("model-modal-add");
  if (button) { button.disabled = true; button.textContent = t("model.adding"); }
  const state = await modelConfigPost("model/add", { providerId, model });
  if (button) { button.disabled = false; button.textContent = t("modal.add"); }
  if (!state) return;
  modelConfigRender(state);
  modelMessage(`${t("model.addedOne", model)}`, false);
  if (typeof loadModels === "function") { try { loadModels(); } catch (_) {} }
  closeModelModal();
}

function providerIdForBase(apibase) {
  const provider = ((modelConfigData || {}).providers || []).find((row) => row.apibase === apibase);
  return provider ? provider.id : "";
}

function modelConfigRender(data) {
  modelConfigData = data;
  const providerScope = $("provider-config-scope");
  if (providerScope) providerScope.textContent = `${t("provider.count", (data.providers || []).length)}`;
  const list = $("provider-config-list");
  if (list) {
    list.innerHTML = (data.providers || []).map((item) => `
      <div class="model-row">
        <div class="model-row-main"><strong>${esc(item.name || item.apibase)}</strong>
          <span>${esc(item.apibase)}</span>
          <span class="tag">${item.hasKey ? "Key " + esc(item.keyMask) : t("provider.noKey")}</span></div>
        <div class="model-row-actions">
          <button type="button" class="ghost-button" data-test-provider="${esc(item.id)}">${t("provider.testBtn")}</button>
          <button type="button" class="ghost-button danger" data-delete-provider="${esc(item.id)}">${t("persona.delete")}</button>
        </div>
      </div>`).join("") || '<span class="muted-tag">' + t("provider.noProviders") + '</span>';
  }
  const scope = $("model-config-scope");
  if (scope) scope.textContent = `${t("provider.modelCount", (data.models || []).length)}`;
  const models = $("model-config-models");
  if (models) {
    models.innerHTML = (data.models || []).map((item) => `
      <div class="model-row">
        <div class="model-row-main"><strong>${esc(item.name || item.model || item.var)}</strong>
          <span>${esc(item.apibase || "—")}</span></div>
        <div class="model-row-actions">
          <button type="button" class="ghost-button" data-test-model="${esc(item.model || item.name)}" data-test-base="${esc(item.apibase || "")}">${t("provider.testBtn")}</button>
          <button type="button" class="ghost-button danger" data-delete-model="${esc(item.var)}">${t("persona.delete")}</button>
        </div>
        ${testLine(providerIdForBase(item.apibase || ""), item.model || item.name)}
      </div>`).join("") || '<span class="muted-tag">' + t("provider.noModels") + '</span>';
  }
  const select = $("model-fetch-provider");
  if (select) {
    const providers = data.providers || [];
    select.innerHTML = providers.map((item) => `<option value="${esc(item.id)}">${esc(item.name || item.apibase)}</option>`).join("")
      || '<option value="">(' + t("provider.addFirst") + ')</option>';
    if (modelFetchState.providerId && providers.some((item) => item.id === modelFetchState.providerId)) {
      select.value = modelFetchState.providerId;
    }
  }
  const anthropic = $("model-config-anthropic");
  if (anthropic) anthropic.textContent = data.anthropicHint || "";
  if (data.parseError) modelMessage(data.parseError, true);
  else if (data.skipped) modelMessage(t("provider.skippedKeyHint"));
  modelConfigRenderFetch();
}

function modelConfigRenderFetch() {
  const box = $("model-config-fetched");
  if (!box) return;
  const state = modelFetchState;
  if (!state.providerId) { box.innerHTML = ""; return; }
  const known = new Set(((modelConfigData || {}).models || []).map((item) => item.model));
  const rows = state.models.map((name) => {
    const added = known.has(name);
    return `
      <div class="model-fetch-item">
        <label><input type="checkbox" value="${esc(name)}" ${added ? "checked disabled" : ""}>
          <span>${esc(name)}${added ? t("provider.alreadyAdded") : ""}</span></label>
        <span class="model-fetch-actions">
          <button type="button" class="ghost-button" data-modal-model="${esc(name)}" data-modal-addable="${added ? "0" : "1"}">${added ? t("common.detail") : t("provider.addDots")}</button>
        </span>
      </div>`;
  }).join("");
  box.innerHTML = `
    <div class="model-fetch-head"><strong>${t("provider.modelsOf", esc(state.providerName || state.providerId), state.models.length)}</strong>
      <button type="button" class="primary-button" id="model-fetch-add">${t("provider.addSelected")}</button>
      <button type="button" class="ghost-button" id="model-fetch-close">${t("btn.collapse")}</button></div>
    <div class="model-fetch-grid">${rows || '<span class="muted-tag">' + t("provider.emptyModelList") + '</span>'}</div>`;
  $("model-fetch-add")?.addEventListener("click", modelConfigAddChecked);
  box.querySelectorAll("[data-modal-model]").forEach((node) => {
    node.addEventListener("click", () => openModelModal({
      providerId: state.providerId,
      providerName: state.providerName,
      model: node.dataset.modalModel,
      addable: node.dataset.modalAddable === "1",
    }));
  });
  $("model-fetch-close")?.addEventListener("click", () => {
    modelFetchState = { providerId: "", providerName: "", models: [] };
    modelConfigRenderFetch();
  });
}

async function modelConfigProviderSave() {
  const name = $("model-provider-name").value.trim();
  const apibase = $("model-provider-base").value.trim();
  const apikey = $("model-provider-key").value.trim();
  if (!apibase) { providerMessage(t("provider.needBase"), true); return; }
  const result = await modelConfigPost("provider/add", { name, apibase, apikey }, providerMessage);
  if (!result) return;
  $("model-provider-key").value = "";
  modelConfigRender(result);
  providerMessage(t("provider.saved"));
}

async function modelConfigProviderDelete(id) {
  if (!confirm(t("confirm.providerDelete", id))) return;
  const result = await modelConfigPost("provider/delete", { id }, providerMessage);
  if (result) { modelConfigRender(result); providerMessage(t("provider.deleted")); }
}

async function modelConfigProviderTest(providerId) {
  providerMessage(t("provider.testing"));
  const result = await modelConfigPost("models/fetch", { providerId }, providerMessage);
  if (!result) return;
  if (!result.ok) { providerMessage(t("model.testFailPrefix", result.error || t("toast.unknownError")), true); return; }
  providerMessage(`${t("provider.okModels", (result.models || []).length)}`);
}

async function modelConfigFetchRun() {
  const select = $("model-fetch-provider");
  const providerId = select ? select.value : "";
  if (!providerId) { modelMessage(t("provider.needProvider"), true); return; }
  const providerName = select.options[select.selectedIndex]?.text || "";
  modelMessage(t("provider.fetching"));
  const result = await modelConfigPost("models/fetch", { providerId });
  if (!result) return;
  if (!result.ok) { modelMessage(t("model.fetchFailPrefix", result.error || t("toast.unknownError")), true); return; }
  modelFetchState = { providerId: result.providerId || providerId, providerName, models: result.models || [] };
  modelConfigRenderFetch();
  modelMessage(`${t("provider.fetched", (result.models || []).length)}`);
}

async function modelTestRaw(providerId, model) {
  try {
    return await fetchJson("/api/model-config/model/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ providerId, model }),
    });
  } catch (error) {
    return { ok: false, error: error.message || String(error) };
  }
}

async function modelConfigTestModel(providerId, model) {
  if (!providerId || !model) { modelMessage(t("provider.needProviderOrModel"), true); return; }
  modelMessage(`${t("model.testingModel", model)}`);
  const result = await modelTestRaw(providerId, model);
  modelTestResults[testKey(providerId, model)] = result;
  modelMessage(result.ok
    ? `${t("model.modelOk", model, result.latencyMs)}${result.reply ? t("model.replySuffix") + result.reply : ""})`
    : t("model.testFailFor", model, result.error || t("toast.unknownError")), !result.ok);
  modelConfigRender(modelConfigData);
}

async function modelConfigTestAll() {
  const models = (modelConfigData || {}).models || [];
  if (!models.length) { modelMessage(t("provider.noModelsShort"), true); return; }
  modelMessage(`${t("provider.testingAll", models.length)}`);
  let okCount = 0;
  for (const item of models) {
    const provider = ((modelConfigData || {}).providers || []).find((row) => row.apibase === item.apibase);
    const providerId = provider ? provider.id : "";
    if (!providerId) continue;
    const name = item.model || item.name;
    const result = await modelTestRaw(providerId, name);
    modelTestResults[testKey(providerId, name)] = result;
    if (result.ok) okCount += 1;
  }
  modelConfigRender(modelConfigData);
  modelMessage(`${t("provider.testDone", okCount, models.length)}`, okCount !== models.length);
}

async function modelConfigAddOne(model) {
  const providerId = modelFetchState.providerId;
  const state = await modelConfigPost("model/add", { providerId, model });
  if (!state) return;
  modelConfigRender(state);
  modelMessage(`${t("model.addedOne", model)}`, false);
  if (typeof loadModels === "function") { try { loadModels(); } catch (_) {} }
}

async function modelConfigAddChecked() {
  const box = $("model-config-fetched");
  const picked = Array.from(box.querySelectorAll('input[type="checkbox"]:checked:not(:disabled)')).map((node) => node.value);
  if (!picked.length) { modelMessage(t("provider.tickOne"), true); return; }
  let state = null;
  for (const model of picked) {
    state = await modelConfigPost("model/add", { providerId: modelFetchState.providerId, model });
    if (!state) return;
  }
  modelConfigRender(state);
  modelMessage(`${t("model.addedMany", picked.length)}`);
  if (typeof loadModels === "function") { try { loadModels(); } catch (_) {} }
}

async function modelConfigModelDelete(variable) {
  if (!confirm(t("confirm.modelDelete", variable))) return;
  const result = await modelConfigPost("model/delete", { var: variable });
  if (result) { modelConfigRender(result); modelMessage(t("provider.modelDeleted")); }
}

$("provider-config-list")?.addEventListener("click", (event) => {
  const testButton = event.target.closest("[data-test-provider]");
  if (testButton) { modelConfigProviderTest(testButton.dataset.testProvider); return; }
  const deleteButton = event.target.closest("[data-delete-provider]");
  if (deleteButton) modelConfigProviderDelete(deleteButton.dataset.deleteProvider);
});
$("model-config-models")?.addEventListener("click", (event) => {
  const testButton = event.target.closest("[data-test-model]");
  if (testButton) {
    const provider = ((modelConfigData || {}).providers || []).find((row) => row.apibase === testButton.dataset.testBase);
    modelConfigTestModel(provider ? provider.id : "", testButton.dataset.testModel);
    return;
  }
  const deleteButton = event.target.closest("[data-delete-model]");
  if (deleteButton) modelConfigModelDelete(deleteButton.dataset.deleteModel);
});
$("model-config-fetched")?.addEventListener("click", (event) => {
  const openButton = event.target.closest("[data-modal-model]");
  if (openButton) {
    openModelModal({
      providerId: modelFetchState.providerId,
      providerName: modelFetchState.providerName,
      model: openButton.dataset.modalModel,
      addable: openButton.dataset.modalAddable === "1",
    });
  }
});
$("model-provider-save")?.addEventListener("click", modelConfigProviderSave);
$("model-fetch-run")?.addEventListener("click", modelConfigFetchRun);
$("model-test-all")?.addEventListener("click", modelConfigTestAll);
$("model-modal-close")?.addEventListener("click", closeModelModal);
$("model-modal-test")?.addEventListener("click", modelModalTest);
$("model-modal-add")?.addEventListener("click", modelModalAdd);
$("model-modal")?.addEventListener("click", (event) => { if (event.target === $("model-modal")) closeModelModal(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeModelModal(); });
$("timeline-board").addEventListener("wheel", (event) => {
  const scroll = event.target.closest(".timeline-scroll");
  if (!scroll) return;
  const rect = scroll.getBoundingClientRect();
  const overVerticalScrollbar = scroll.scrollHeight > scroll.clientHeight && event.clientX >= rect.right - 16;
  const overHorizontalScrollbar = scroll.scrollWidth > scroll.clientWidth && event.clientY >= rect.bottom - 16;
  if (overVerticalScrollbar || overHorizontalScrollbar) return;
  event.preventDefault();
  changeTimelineZoom(event.deltaY < 0 ? 1 : -1, event);
}, { passive: false });
$("timeline-board").addEventListener("pointerover", (event) => {
  const target = event.target.closest("[data-timeline-tooltip]");
  if (target) showTimelineTooltip(target, event.clientX, event.clientY);
});
$("timeline-board").addEventListener("pointermove", (event) => {
  const target = event.target.closest("[data-timeline-tooltip]");
  if (target) showTimelineTooltip(target, event.clientX, event.clientY);
});
$("timeline-board").addEventListener("pointerout", (event) => { if (event.target.closest("[data-timeline-tooltip]")) hideTimelineTooltip(); });
$("timeline-board").addEventListener("focusin", (event) => {
  const target = event.target.closest("[data-timeline-tooltip]");
  if (target) { const rect = target.getBoundingClientRect(); showTimelineTooltip(target, rect.left + rect.width / 2, rect.top + rect.height / 2); }
});
$("timeline-board").addEventListener("focusout", hideTimelineTooltip);
$("diary-search").addEventListener("input", renderDiaryIndex);
$("diary-month").addEventListener("change", (event) => {
  diaryMonth = event.target.value;
  renderDiaryIndex();
  const first = diaryData?.entries?.find((entry) => entry.date.startsWith(diaryMonth));
  if (first) openDiary(first.date);
});
$("diary-prev").addEventListener("click", () => moveDiary(1));
$("diary-next").addEventListener("click", () => moveDiary(-1));
$("sop-scope")?.addEventListener("change", (event) => {
  sopScope = event.target.value || "sop-user";
  localStorage.setItem("g4w-sop-scope", sopScope);
  currentSopPath = "";
  sopData = null;
  sopExpanded = new Set();
  if ($("sop-title")) $("sop-title").textContent = t("sop.pick");
  if ($("sop-meta")) $("sop-meta").innerHTML = "";
  if ($("sop-content")) $("sop-content").innerHTML = '<div class="empty-state">' + t("sop.emptyHint") + '</div>';
  loadSopIndex(true);
});
$("sop-search")?.addEventListener("input", () => {
  renderSopIndex();
});
$("sop-prev")?.addEventListener("click", () => moveSop(-1));
$("sop-next")?.addEventListener("click", () => moveSop(1));
$("knowledge-search").addEventListener("input", (event) => { knowledgeQuery = event.target.value; renderKnowledgeCatalog(dashboardData?.knowledge?.items || []); });
$("knowledge-tag").addEventListener("change", (event) => { knowledgeTag = event.target.value; renderKnowledgeCatalog(dashboardData?.knowledge?.items || []); });
$("settings-form").addEventListener("submit", saveSettings);
setupUiPrefControls();
$("settings-form").addEventListener("input", (event) => { if (event.target.closest("[data-model-target]")) return; settingsDirty = true; $("settings-message").textContent = t("env.dirty"); });
document.querySelectorAll("[data-model-target]").forEach((select) => select.addEventListener("change", () => switchModel(select)));
$("prompt-refresh").addEventListener("click", loadPrompt);
$("overview-start").addEventListener("click", startG4W);
$("terminal-follow").addEventListener("click", toggleTerminalFollow);
$("terminal-clear").addEventListener("click", () => { const body = $("terminal-body"); if (body) body.innerHTML = ""; });
$("worker-monitor-follow").addEventListener("click", () => {
  const btn = $("worker-monitor-follow");
  btn.classList.toggle("active");
  if (btn.classList.contains("active")) {
    const body = $("worker-monitor-body");
    if (body) body.scrollTop = body.scrollHeight;
  }
});
$("worker-monitor-clear").addEventListener("click", () => { const body = $("worker-monitor-body"); if (body) body.innerHTML = ""; });
$("worker-monitor-all").addEventListener("click", clearWorkerMonitorFilter);
document.querySelectorAll("[data-todo-tab]").forEach((button) => button.addEventListener("click", () => {
  todoTab = button.dataset.todoTab;
  todoPage = 1;
  renderTodoPage(true);
}));
$("todo-prev").addEventListener("click", () => { if (todoPage > 1) { todoPage -= 1; renderTodoPage(true); } });
$("todo-next").addEventListener("click", () => { todoPage += 1; renderTodoPage(true); });
document.addEventListener("click", (event) => {
  const close = event.target.closest("[data-close-drawer]"); if (close) { closeDrawer(); return; }
  const nav = event.target.closest(".nav-item"); if (nav) { setSidebarOpen(false); if (location.hash === nav.getAttribute("href")) routeFromHash(); }
  const palette = event.target.closest("[data-palette-choice]");
  if (palette) {
    const choice = palette.dataset.paletteChoice;
    localStorage.setItem("g4w-palette", choice);
    applyAppearance(document.documentElement.dataset.themeMode || "system", choice);
    return;
  }
  const timelineModeButton = event.target.closest("[data-timeline-mode]");
  if (timelineModeButton) {
    timelineMode = timelineModeButton.dataset.timelineMode;
    document.querySelectorAll("[data-timeline-mode]").forEach((button) => button.classList.toggle("active", button === timelineModeButton));
    renderTimeline();
    return;
  }
  const timelineZoomButton = event.target.closest("[data-timeline-zoom]");
  if (timelineZoomButton) {
    const action = timelineZoomButton.dataset.timelineZoom;
    changeTimelineZoom(action === "reset" ? 0 : action === "in" ? 1 : -1);
    return;
  }
  const timelineFilter = event.target.closest("[data-timeline-filter]");
  if (timelineFilter) {
    timelineCategoryFilter = timelineCategoryFilter === timelineFilter.dataset.timelineFilter ? "" : timelineFilter.dataset.timelineFilter;
    renderTimeline();
    return;
  }
  const timelineEvent = event.target.closest("[data-timeline-event]");
  if (timelineEvent) { openTimelineEvent(timelineEvent.dataset.timelineEvent, timelineEvent.dataset.timelineDate); return; }
  const diary = event.target.closest("[data-diary-date]"); if (diary) { openDiary(diary.dataset.diaryDate); return; }
  const sopDir = event.target.closest("[data-sop-dir]"); if (sopDir) { toggleSopDir(sopDir.dataset.sopDir); return; }
  const sopFile = event.target.closest("[data-sop-path]"); if (sopFile) { openSop(sopFile.dataset.sopPath); return; }
  const worker = event.target.closest("[data-worker-id]"); if (worker) { openWorker(worker.dataset.workerId); return; }
  const memorySection = event.target.closest("[data-memory-section]");
  if (memorySection) { openMemorySection(memorySection.dataset.memorySection); return; }
  const memoryCompact = event.target.closest("[data-memory-compact]");
  if (memoryCompact) { compactMemory(memoryCompact.dataset.memoryCompact); return; }
  const knowledge = event.target.closest("[data-knowledge-id]"); if (knowledge) { openKnowledge(knowledge.dataset.knowledgeId); return; }
  const knowledgeTab = event.target.closest("[data-knowledge-tab]");
  if (knowledgeTab) {
    document.querySelectorAll("[data-knowledge-tab]").forEach((button) => button.classList.toggle("active", button === knowledgeTab));
    document.querySelectorAll("[data-knowledge-panel]").forEach((panel) => { panel.hidden = panel.dataset.knowledgePanel !== knowledgeTab.dataset.knowledgeTab; });
    return;
  }
  const eventFilterButton = event.target.closest("[data-event-filter]");
  if (eventFilterButton) {
    eventFilter = eventFilterButton.dataset.eventFilter;
    document.querySelectorAll("[data-event-filter]").forEach((button) => button.classList.toggle("active", button === eventFilterButton));
    renderSystemEvents(dashboardData?.events || []);
    return;
  }
  const promptTab = event.target.closest("[data-prompt-tab]");
  if (promptTab) {
    document.querySelectorAll("[data-prompt-tab]").forEach((button) => button.classList.toggle("active", button === promptTab));
    document.querySelectorAll("[data-prompt-panel]").forEach((panel) => { panel.hidden = panel.dataset.promptPanel !== promptTab.dataset.promptTab; });
    return;
  }
  const promptNode = event.target.closest("[data-prompt-node]");
  if (promptNode && promptData) {
    let node = [...(promptData.components || []), promptData.thinkingNode].filter(Boolean)[Number(promptNode.dataset.promptNode)];
    if (promptNode.dataset.promptChild != null) node = (node.children || [])[Number(promptNode.dataset.promptChild)];
    if (node) selectPromptNode(node, promptNode);
  }
});
window.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeDrawer(); setSidebarOpen(false); } });
window.addEventListener("hashchange", routeFromHash);
window.addEventListener("resize", () => { if (window.innerWidth > 760) setSidebarOpen(false); });
matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => { if ((document.documentElement.dataset.themeMode || "system") === "system") applyAppearance("system", document.documentElement.dataset.palette || "wechat"); });

applyAppearance(localStorage.getItem("g4w-theme") || "system", localStorage.getItem("g4w-palette") || "wechat");
$("login-button").addEventListener("click", submitLogin);
$("login-password").addEventListener("keydown", (event) => { if (event.key === "Enter") submitLogin(); });
$("setup-button").addEventListener("click", submitSetup);
$("setup-password2").addEventListener("keydown", (event) => { if (event.key === "Enter") submitSetup(); });
$("password-save").addEventListener("click", submitPasswordChange);
$("logout-all").addEventListener("click", logoutAll);
$("theme-save").addEventListener("click", saveTheme);
$("theme-reset").addEventListener("click", resetTheme);
// 多标签警告：Chrome 对同一域名（127.0.0.1:18180）只有 6 条连接配额，
// 每个标签页都占用 1 条 SSE 长连接——多开会让 API 请求排队超时（实证事故）
if ("BroadcastChannel" in window) {
  const tabChannel = new BroadcastChannel("g4w-dashboard-tabs");
  let tabWarned = false;
  tabChannel.onmessage = () => {
    if (tabWarned) return;
    tabWarned = true;
    showToast(t("app.multiTab"), true);
  };
  tabChannel.postMessage("tab-open");
}
document.querySelectorAll(".theme-mode-tabs [data-theme-mode]").forEach((button) => button.addEventListener("click", () => {
  themeEditMode = button.dataset.themeMode;
  document.querySelectorAll(".theme-mode-tabs [data-theme-mode]").forEach((b) => b.classList.toggle("active", b === button));
  renderThemeEditor();
}));
ensureAuth().then((ok) => {
  if (!ok) return;
  routeFromHash();
  refresh();
  connectEvents();
});
setInterval(refresh, 30000);



// ---------- 终端：cmd 式选中复制（选中期间不刷新、不裁剪、不滚动） ----------
function ensureTerminalPauseHint() {
  if (document.getElementById("terminal-pause-hint")) return;
  const body = document.getElementById("terminal-body");
  const header = body ? body.closest(".terminal-card")?.querySelector(".terminal-header") : null;
  if (!header) return;
  const hint = document.createElement("span");
  hint.id = "terminal-pause-hint";
  hint.className = "terminal-pause-hint";
  hint.hidden = true;
  hint.textContent = t("terminal.paused");
  header.appendChild(hint);
}

function updateTerminalPauseFromSelection() {
  const body = document.getElementById("terminal-body");
  if (!body) return;
  const sel = window.getSelection ? window.getSelection() : null;
  let paused = false;
  if (sel && sel.rangeCount && !sel.isCollapsed) {
    let node = sel.anchorNode;
    if (node && node.nodeType !== 1) node = node.parentElement;
    paused = !!(node && body.contains(node));
  }
  if (paused === terminalPaused) return;
  terminalPaused = paused;
  ensureTerminalPauseHint();
  const hint = document.getElementById("terminal-pause-hint");
  if (hint) hint.hidden = !paused;
  if (!paused && terminalId) { stopTerminalPoll(); startTerminalPoll(); }
}

document.addEventListener("selectionchange", () => {
  window.clearTimeout(updateTerminalPauseFromSelection._t);
  updateTerminalPauseFromSelection._t = window.setTimeout(updateTerminalPauseFromSelection, 120);
});
