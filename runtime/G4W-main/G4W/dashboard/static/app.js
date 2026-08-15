const $ = (id) => document.getElementById(id);
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
  if (!username) { errorEl.textContent = "请输入用户名"; errorEl.hidden = false; return; }
  if (password.length < 8) { errorEl.textContent = "密码至少 8 位"; errorEl.hidden = false; return; }
  if (password !== password2) { errorEl.textContent = "两次输入的密码不一致"; errorEl.hidden = false; return; }
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
    errorEl.textContent = data.error || "设置失败";
    errorEl.hidden = false;
  } catch {
    errorEl.textContent = "无法连接服务器";
    errorEl.hidden = false;
  }
}

async function submitLogin() {
  const username = $("login-username").value.trim();
  const password = $("login-password").value;
  const errorEl = $("login-error");
  if (!username || !password) {
    errorEl.textContent = "请输入用户名和密码";
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
    errorEl.textContent = data.error || "用户名或密码错误";
    errorEl.hidden = false;
  } catch {
    errorEl.textContent = "无法连接服务器";
    errorEl.hidden = false;
  }
}

async function submitPasswordChange() {
  const oldPwd = $("setting-old-password").value;
  const newPwd = $("setting-new-password").value;
  const newPwd2 = $("setting-new-password2").value;
  const messageEl = $("password-message");
  if (newPwd.length < 8) { messageEl.textContent = "新密码至少 8 位"; messageEl.style.color = "#ff6b6b"; return; }
  if (newPwd !== newPwd2) { messageEl.textContent = "两次输入的新密码不一致"; messageEl.style.color = "#ff6b6b"; return; }
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
      messageEl.textContent = "密码已更新，下次登录请使用新密码";
      messageEl.style.color = "";
    } else {
      messageEl.textContent = data.error || "修改失败";
      messageEl.style.color = "#ff6b6b";
    }
  } catch {
    messageEl.textContent = "无法连接服务器";
    messageEl.style.color = "#ff6b6b";
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
    const text = `${timelineCategories[category] || category} ${pct}%`;
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
let modelsLoading = false;
let eventFilter = "all";
let knowledgeQuery = "";
let knowledgeTag = "";

const fallback = {
  updatedAt: Date.now() / 1000,
  system: { name: "G4W", user: "本地用户", status: "standby", model: "--", vector: false },
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
  if (Array.isArray(value) && value.length > maxItems) output = [...value.slice(0, maxItems), { note: `仅展示前 ${maxItems} 条，原始数据共 ${value.length} 条` }];
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

const pageTitles = { overview: "Overview", workers: "Workers", memory: "Memory", timeline: "Timeline", diary: "Diary", todo: "待办", services: "服务与终端", knowledge: "Knowledge", events: "System Activity", environment: "Environment", prompt: "System Prompt" };

function setSidebarOpen(open) {
  $("sidebar").classList.toggle("open", open);
  document.body.classList.toggle("sidebar-open", open);
  $("menu-button").setAttribute("aria-expanded", String(open));
}

function routeTo(page) {
  const target = pageTitles[page] ? page : "overview";
  document.querySelectorAll("[data-page-view]").forEach((view) => view.classList.toggle("active", view.dataset.pageView === target));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === target));
  $("page-name").textContent = pageTitles[target];
  document.title = `${pageTitles[target]} · G4W Control Center`;
  setSidebarOpen(false);
  window.scrollTo(0, 0);
  if (target === "prompt" && !promptData) loadPrompt();
  if (target === "timeline" && !timelineData) loadTimeline();
  if (target === "diary" && !diaryData) loadDiary();
  if (target === "environment") loadModels();
  if (target === "services") { renderServicesPage(true); if (terminalId) startTerminalPoll(); }
  else if (terminalTimer) stopTerminalPoll();
  if (target === "workers") startWorkerMonitorPoll();
  else stopWorkerMonitorPoll();
}

function routeFromHash() { routeTo((location.hash || "#overview").slice(1)); }

// 自定义主题:变量清单(shared=通用, mode=分深浅两套)
const THEME_EDITOR_VARS = {
  shared: [
    ["accent-rgb", "强调色 RGB"], ["wechat", "主色"], ["wechat-strong", "主色(深)"], ["wechat-soft", "主色(淡)"], ["line-strong", "主色描边"], ["radius", "界面圆角(px)"],
    ["danger", "危险"], ["warning", "警告"], ["blue", "蓝色"], ["purple", "紫色"],
    ["cat-life", "分类·生活"], ["cat-work", "分类·工作"], ["cat-study", "分类·学习"], ["cat-exercise", "分类·运动"], ["cat-entertainment", "分类·娱乐"],
    ["cat-health", "分类·健康"], ["cat-social", "分类·社交"], ["cat-care", "分类·照料"], ["cat-travel", "分类·出行"], ["cat-rest", "分类·休息"],
  ],
  mode: [
    ["bg", "背景"], ["sidebar", "侧边栏"], ["panel", "面板"], ["panel-strong", "面板(实)"], ["field", "输入框"], ["line", "分隔线"],
    ["text", "文字"], ["soft", "次要文字"], ["muted", "弱文字"], ["faint", "最弱文字"], ["overlay", "遮罩"], ["shadow", "阴影"],
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
  $("theme-button").title = `主题：${mode === "system" ? "跟随系统" : mode === "light" ? "浅色" : "深色"}`;
  $("theme-mode-select").value = mode;
  document.querySelectorAll("[data-palette-choice]").forEach((button) => button.classList.toggle("active", button.dataset.paletteChoice === resolvedPalette));
  const paletteLabel = resolvedPalette === "neko" ? "Neko 粉" : resolvedPalette === "argon" ? "简洁蓝" : resolvedPalette === "custom" ? "自定义" : "微信绿";
  const modeLabel = mode === "system" ? "跟随系统" : mode === "light" ? "浅色" : "深色";
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
    ? `<button class="worker-output-btn" data-worker-output="${esc(worker.dir)}" data-worker-archive="${esc(worker.archivePath)}" data-worker-id="${esc(worker.id)}" data-worker-label="${esc(worker.topic)}" title="在右侧监视器查看该 worker 的输出（未运行则显示历史）">输出</button>`
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
    `<label class="theme-row"><span>${label}<small>--${key}</small></span><input type="text" data-theme-var="${key}" data-theme-group="${group}" value="${esc(value)}" placeholder="默认" spellcheck="false" /></label>`;
  const html =
    `<div class="theme-group-label">通用(深浅共用)</div>` +
    THEME_EDITOR_VARS.shared.map(([key, label]) => row(key, label, shared[key] || "", "shared")).join("") +
    `<div class="theme-group-label">${mode === "dark" ? "深色" : "浅色"}模式界面</div>` +
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
    if (!res.ok) throw new Error(res.error || "保存失败");
    if (dashboardData && dashboardData.configuration) dashboardData.configuration.theme = theme;
    if (document.documentElement.dataset.palette === "custom") applyAppearance(document.documentElement.dataset.themeMode || "system", "custom");
    messageEl.textContent = "主题已保存，立即生效";
    messageEl.style.color = "";
  } catch (error) {
    messageEl.textContent = error.message || "保存失败";
    messageEl.style.color = "#ff6b6b";
  }
}

async function resetTheme() {
  const messageEl = $("theme-message");
  try {
    const res = await fetchJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ theme: { dark: {}, light: {}, shared: {} } }) });
    if (!res.ok) throw new Error(res.error || "恢复失败");
    if (dashboardData && dashboardData.configuration) dashboardData.configuration.theme = { dark: {}, light: {}, shared: {} };
    renderThemeEditor();
    if (document.documentElement.dataset.palette === "custom") applyAppearance(document.documentElement.dataset.themeMode || "system", "custom");
    messageEl.textContent = "已恢复默认";
    messageEl.style.color = "";
  } catch (error) {
    messageEl.textContent = error.message || "恢复失败";
    messageEl.style.color = "#ff6b6b";
  }
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
    "setting-input": config.inputCaptureEnabled,
    "setting-checkin": config.checkinEnabled,
    "setting-vector": config.vectorEnabled,
    "setting-web-search": config.webSearchEnabled
  };
  Object.entries(checks).forEach(([id, value]) => { if ($(id)) $(id).checked = Boolean(value); });
  renderThemeEditor();
  loadEmbeddingConfig();
}

function compactEventRows(items) {
  return items.map((event) => `<div class="event-row"><span class="event-dot ${event.status === "failed" ? "danger" : ""}"></span><div><strong>${esc(event.type)}</strong><span>${esc(event.detail || event.rawType || "系统状态发生变化")}</span></div><time>${esc(event.time)}</time></div>`).join("");
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
    return `<article class="system-event"><div class="system-event-icon ${esc(category)}">${eventIcon(event.category)}</div><div class="system-event-copy"><strong>${esc(event.type)}</strong><span>${esc(event.detail || "系统状态发生变化")}</span><small>${esc(event.rawType || "system.event")}${event.relatedId ? ` · ${esc(event.relatedId)}` : ""}</small></div><div class="system-event-meta"><b>${esc(statusLabel(event.status || "done"))}</b>${esc(event.time || "--")}</div></article>`;
  }).join("") : '<div class="empty-state">当前分类暂无系统动态</div>';
}

function renderKnowledgeCatalog(items) {
  const tagSelect = $("knowledge-tag");
  const currentOptions = new Set(Array.from(tagSelect.options).map((option) => option.value));
  const tags = [...new Set(items.flatMap((item) => item.tags || []).filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b), "zh-CN"));
  if (tags.some((tag) => !currentOptions.has(String(tag))) || tagSelect.options.length !== tags.length + 1) {
    tagSelect.innerHTML = '<option value="">全部标签</option>' + tags.map((tag) => `<option value="${esc(tag)}">${esc(tag)}</option>`).join("");
    tagSelect.value = knowledgeTag;
  }
  const query = knowledgeQuery.trim().toLowerCase();
  const visible = items.filter((item) => {
    const haystack = [item.title, item.id, ...(item.tags || [])].join(" ").toLowerCase();
    return (!query || haystack.includes(query)) && (!knowledgeTag || (item.tags || []).includes(knowledgeTag));
  });
  $("knowledge-page-list").innerHTML = visible.length ? visible.map((item) => `<div class="data-row clickable" data-knowledge-id="${esc(item.id)}"><div class="data-icon blue">▣</div><div class="data-copy"><strong>${esc(item.title)}</strong><span>${(item.tags || []).map((tag) => `<i class="tag">${esc(tag)}</i>`).join("") || "无标签"}</span><small>${esc(item.id)}</small></div><div class="data-meta"><b>${esc(item.chunks)} CHUNKS</b><span>${esc(item.updatedAt)}</span></div></div>`).join("") : '<div class="empty-state">没有符合筛选条件的文档</div>';
}

function render(data) {
  dashboardData = data;
  const system = data.system || fallback.system;
  const metrics = data.metrics || fallback.metrics;
  if (document.documentElement.dataset.palette === "custom") applyCustomTheme(dashboardData?.configuration?.theme);
  const memory = data.memory || fallback.memory;
  const knowledge = data.knowledge || fallback.knowledge;
  const configuration = data.configuration || fallback.configuration;
  const workers = data.workers || [];
  const events = data.events || [];
  const online = system.status === "online";

  $("user-name").textContent = system.user || "本地用户";
  $("top-status").textContent = statusLabel(system.status);
  $("hero-status").textContent = `CORE ${statusLabel(system.status)}`;
  $("hero-model").textContent = `${system.model || "未配置模型"} · ${system.vector ? "VECTOR ON" : "KEYWORD MODE"}`;
  $("conductor-node").textContent = online ? "ONLINE" : "STANDBY";
  document.querySelector(".node-core").classList.toggle("online", online);
  $("running-workers").textContent = metrics.runningWorkers ?? 0;
  $("worker-total").textContent = `${metrics.workers ?? 0} 个已登记`;
  $("memory-count").textContent = metrics.memoryProfiles ?? 0;
  $("memory-backups").textContent = `${metrics.memoryBackups ?? 0} 个历史版本`;
  $("knowledge-count").textContent = knowledge.documents ?? metrics.knowledgeDocuments ?? 0;
  $("chunk-count").textContent = `${knowledge.chunks ?? metrics.knowledgeChunks ?? 0} 个内容分块`;
  $("event-count").textContent = metrics.pendingEvents ?? 0;
  $("worker-node").textContent = `${metrics.runningWorkers ?? 0} ACTIVE`;
  document.querySelector(".node-worker").classList.toggle("busy", (metrics.runningWorkers ?? 0) > 0);
  $("memory-node").textContent = `${metrics.memoryProfiles ?? 0} PROFILES`;
  $("kb-node").textContent = `${knowledge.documents ?? 0} DOCS`;
  $("context-model").textContent = system.model || "--";
  $("context-vector").textContent = system.vector ? "已开启" : "未开启";
  $("context-memory").textContent = memory.root || "--";
  $("context-kb").textContent = knowledge.root || "--";
  $("last-updated").textContent = new Date((data.updatedAt || Date.now()/1000)*1000).toLocaleTimeString("zh-CN", { hour12: false });
  const nextCheckin = Number(data.nextCheckinAt) || 0;
  const nextCheckinEl = $("next-checkin-time");
  if (nextCheckinEl) {
    nextCheckinEl.textContent = nextCheckin > 0
      ? new Date(nextCheckin * 1000).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" })
      : "未安排";
  }
  $("footer-year").textContent = new Date().getFullYear();
  $("sidebar-status").textContent = "DASHBOARD ONLINE";

  $("worker-list").innerHTML = workers.length ? workers.slice(0,8).map((worker) => workerRow(worker, true)).join("") : '<div class="empty-state">当前没有 Worker 记录</div>';
  $("workers-page-total").textContent = metrics.workers ?? workers.length;
  $("workers-page-running").textContent = metrics.runningWorkers ?? 0;
  $("workers-page-visible").textContent = workers.length;
  $("workers-page-badge").textContent = `${metrics.workers ?? workers.length} REGISTERED`;
  $("worker-page-list").innerHTML = workers.length ? workers.map((worker) => workerRow(worker)).join("") : '<div class="empty-state">暂无 Worker 数据</div>';
  $("worker-page-list").querySelectorAll("[data-worker-output]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      selectWorkerMonitor(btn.dataset.workerOutput, btn.dataset.workerArchive, btn.dataset.workerId, btn.dataset.workerLabel);
    });
  });

  $("event-list").innerHTML = events.length ? compactEventRows(events.slice(0,8)) : '<div class="empty-state">还没有活动事件</div>';
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

const timelineCategories = {
  life: "生活", work: "工作", study: "学习", exercise: "运动", entertainment: "娱乐",
  health: "健康", social: "社交", care: "照料", travel: "出行", rest: "休息"
};

function dateObject(value) { return new Date(`${value}T12:00:00`); }
function isoDate(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
function addDays(value, amount) { const date = dateObject(value); date.setDate(date.getDate() + amount); return isoDate(date); }
function shortDate(value) { return dateObject(value).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" }); }
function weekday(value) { return dateObject(value).toLocaleDateString("zh-CN", { weekday: "short" }); }
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
  return `<div class="hour-labels">${Array.from({ length: 24 }, (_, hour) => `<span class="hour-label" style="top:${hour * timelineSlotHeight}px">${String(hour).padStart(2, "0")}:00</span>`).join("")}</div>`;
}
function timelineTooltipText(event) {
  const category = timelineCategories[event.categoryId] || event.categoryId || "其他";
  return `${event.date}|${eventClock(event.startAt)}–${eventClock(event.endAt)} · ${formatDuration(eventMinutes(event))}|${category}|${event.note || "没有附加说明"}`;
}
function timelineBlock(event) {
  const dayHeight = timelineSlotHeight * 24;
  const top = clockMinutes(event.startAt) / 60 * timelineSlotHeight;
  const height = Math.max(24, Math.min(dayHeight - top, eventMinutes(event) / 60 * timelineSlotHeight));
  return `<button class="timeline-event-block" data-timeline-tooltip="${esc(timelineTooltipText(event))}" data-timeline-event="${esc(event.id)}" data-timeline-date="${esc(event.date)}" style="top:${top}px;height:${height}px;--event-color:${timelineColor(event.categoryId)}"><strong>${esc(event.title)}<span class="block-duration"> | ${formatDuration(eventMinutes(event))}</span></strong><small>${eventClock(event.startAt)}–${eventClock(event.endAt)} · ${esc(timelineCategories[event.categoryId] || event.categoryId || "其他")}</small></button>`;
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
    return `<div class="month-row"><div class="month-day">${shortDate(date)}<small>${weekday(date)} · ${events.length} 条</small></div><div class="month-track">${blocks}</div></div>`;
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
  $("timeline-focus-title").textContent = timelineMode === "day" ? `${shortDate(timelineDate)} 怎么过` : timelineMode === "week" ? "这一周怎么过" : `${timelineDate.slice(0, 7)} 月度节奏`;
  $("timeline-focus-subtitle").textContent = timelineMode === "day" ? "时间竖着走，短记录也能看清。" : timelineMode === "week" ? "横向比较每天的活动密度与空白。" : "按天扫描整月的时间块分布。";
  $("timeline-zoom-label").textContent = timelineMode === "month" ? `${Math.round(timelineMonthZoom * 100)}%` : `${Math.round(timelineSlotHeight / 30 * 100)}%`;
  $("timeline-board").innerHTML = allEvents.length ? (timelineMode === "day" ? dayBoard(dates) : timelineMode === "week" ? weekBoard(dates) : monthBoard(dates)) : '<div class="empty-state">当前范围没有时间轴事件</div>';
  $("timeline-legend").innerHTML = categories.map(([category]) => `<span class="legend-chip"><i style="background:${timelineColor(category)}"></i>${esc(timelineCategories[category] || category)}</span>`).join("");
  let cursor = 0;
  const allMinutes = allEvents.reduce((total, event) => total + eventMinutes(event), 0);
  $("timeline-donut").innerHTML = renderDonut(categories, allMinutes);
  $("timeline-donut").dataset.total = formatDuration(allMinutes);
  const filterLabel = timelineCategoryFilter ? timelineCategories[timelineCategoryFilter] || timelineCategoryFilter : "全部";
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
    return `<button class="distribution-row ${active ? "active" : ""} ${dimmed ? "dimmed" : ""}" data-timeline-filter="${esc(category)}"><i style="background:${timelineColor(category)}"></i><span>${esc(timelineCategories[category] || category)}</span><small>${formatDuration(value)} · ${Math.round(value / (allEvents.reduce((total, event) => total + eventMinutes(event), 0) || 1) * 100)}%</small></button>`;
  }).join("") : '<div class="empty-state">暂无分类数据</div>';
  const dayTotals = dates.map((date) => filterTimelineEvents(eventsForDates([date])).reduce((total, event) => total + eventMinutes(event), 0));
  const peak = Math.max(1, ...dayTotals);
  const barColor = timelineCategoryFilter ? timelineColor(timelineCategoryFilter) : "";
  const gridMax = Math.max(1, Math.ceil(peak / 60));
  const gridFmt = (hours) => hours === 0 ? "0" : hours < 1 ? `${Math.round(hours * 60)}m` : hours % 1 === 0 ? `${hours}h` : `${hours.toFixed(1)}h`;
  const gridLines = [0, 1, 2, 3, 4].map((i) => `<div class="trend-grid-line" style="bottom:${i * 25}%"><span>${gridFmt(gridMax * i / 4)}</span></div>`).join("");
  $("timeline-trend").innerHTML = `<div class="trend-grid">${gridLines}</div>` + dates.map((date, index) => `<div class="trend-bar" style="height:${Math.max(2, dayTotals[index] / peak * 100)}%;${barColor ? `--bar-color:${barColor};` : ""}" data-value="${formatDuration(dayTotals[index])}"><span>${timelineMode === "month" ? date.slice(8) : shortDate(date)}</span></div>`).join("");
  $("timeline-event-list").innerHTML = events.length ? events.map((event) => `<button class="timeline-event-card" data-timeline-event="${esc(event.id)}" data-timeline-date="${esc(event.date)}" style="--event-color:${timelineColor(event.categoryId)};--event-mix:${timelineMix(event)}%"><time>${esc(event.date)} · ${eventClock(event.startAt)}–${eventClock(event.endAt)}</time><strong>${esc(event.title)}<span class="block-duration"> | ${formatDuration(eventMinutes(event))}</span></strong><span>${esc(event.note || `${timelineCategories[event.categoryId] || event.categoryId || "其他"} · ${formatDuration(eventMinutes(event))}`)}</span></button>`).join("") : '<div class="empty-state">当前范围暂无事件</div>';
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
  tooltip.innerHTML = `<strong>${esc(target.querySelector("strong,span")?.textContent || "时间轴事件")}</strong><span>${esc(parts.slice(0, 3).filter(Boolean).join(" · "))}</span><span>${esc(parts.slice(3).join("|") || "没有附加说明")}</span>`;
  tooltip.hidden = false;
  const width = tooltip.offsetWidth;
  const height = tooltip.offsetHeight;
  tooltip.style.left = `${Math.max(12, Math.min(window.innerWidth - width - 12, clientX + 14))}px`;
  tooltip.style.top = `${Math.max(12, Math.min(window.innerHeight - height - 12, clientY + 14))}px`;
}

function hideTimelineTooltip() { $("timeline-tooltip").hidden = true; }

async function loadTimeline() {
  lastTimelineAttempt = Date.now();
  $("timeline-board").innerHTML = '<div class="empty-state">正在读取时间轴…</div>';
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
  const html = `<div class="detail-pills"><span>${esc(date)}</span><span>${eventClock(event.startAt)}–${eventClock(event.endAt)}</span><span>${formatDuration(eventMinutes(event))}</span><span>${esc(timelineCategories[event.categoryId] || event.categoryId || "其他")}</span>${tags}</div><section class="drawer-section"><h3>记录</h3><p>${esc(event.note || "这条时间块没有附加说明。")}</p></section><section class="drawer-section"><h3>分类</h3><p>${esc(event.subcategoryId || event.categoryId || "未分类")}</p></section>`;
  openDrawer(event.title || "时间轴事件", "TIMELINE EVENT", html, timelineColor(event.categoryId));
}

function renderDiaryIndex() {
  const entries = (diaryData || {}).entries || [];
  const query = $("diary-search").value.trim().toLowerCase();
  const visible = entries.filter((entry) => (!diaryMonth || entry.date.startsWith(diaryMonth)) && (!query || [entry.date, entry.title, entry.excerpt].join(" ").toLowerCase().includes(query)));
  const monthCount = entries.filter((entry) => !diaryMonth || entry.date.startsWith(diaryMonth)).length;
  $("diary-page-badge").textContent = `${monthCount} / ${entries.length} DAYS`;
  $("diary-date-list").innerHTML = visible.length ? visible.map((entry) => `<button class="diary-date ${entry.date === currentDiaryDate ? "active" : ""}" data-diary-date="${entry.date}"><time>${entry.date}</time><strong>${esc(entry.title || "当日日记")}</strong><span>${esc(entry.excerpt || "")}</span></button>`).join("") : '<div class="empty-state">没有符合搜索条件的日记</div>';
}

function renderDiaryMonths() {
  const counts = {};
  for (const entry of (diaryData?.entries || [])) counts[entry.date.slice(0, 7)] = (counts[entry.date.slice(0, 7)] || 0) + 1;
  const months = Object.keys(counts).sort().reverse();
  diaryMonth = diaryMonth || months[0] || "";
  $("diary-month").innerHTML = months.map((month) => {
    const [year, number] = month.split("-");
    return `<option value="${month}">${year}年${Number(number)}月（${counts[month]}篇）</option>`;
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
  $("diary-title").textContent = "正在读取…";
  $("diary-content").innerHTML = '<div class="empty-state">加载中…</div>';
  try {
    const data = await fetchJson(`/api/diary?date=${encodeURIComponent(date)}`);
    const entry = (diaryData?.entries || []).find((item) => item.date === date);
    $("diary-title").textContent = entry?.title || `${date} 日记`;
    $("diary-meta").innerHTML = [`日期 ${date}`, `${data.sections || 0} 个章节`, data.updatedAt || "--", data.source || ""].filter(Boolean).map((value) => `<span>${esc(value)}</span>`).join("");
    $("diary-content").innerHTML = renderMarkdown(data.content || "这一天没有写下内容。");
    const index = (diaryData?.entries || []).findIndex((item) => item.date === date);
    $("diary-prev").disabled = index >= (diaryData?.entries || []).length - 1;
    $("diary-next").disabled = index <= 0;
  } catch (error) { $("diary-title").textContent = "日记读取失败"; $("diary-content").innerHTML = `<div class="empty-state">${esc(error.message)}</div>`; }
}

function moveDiary(direction) {
  const entries = diaryData?.entries || [];
  const index = entries.findIndex((entry) => entry.date === currentDiaryDate);
  const target = entries[index + direction];
  if (target) openDiary(target.date);
}

async function openKnowledge(documentId) {
  openDrawer("正在读取文档", "KNOWLEDGE DETAIL", '<div class="empty-state">加载正文与分块…</div>');
  try {
    const data = await fetchJson(`/api/knowledge?id=${encodeURIComponent(documentId)}`);
    const chunks = (data.chunks || []).map((chunk, index) => `<article class="chunk-card"><header><span>#${index + 1}${chunk.section ? ` · ${esc(chunk.section)}` : ""}</span><span>${chunk.page != null ? `PAGE ${esc(chunk.page)}` : esc(chunk.id)}</span></header><p>${esc(chunk.text)}${chunk.truncated ? "\n…" : ""}</p></article>`).join("") || '<div class="empty-state">没有找到文档分块</div>';
    const sourceRows = [["文档 ID", data.id], ["来源文件", data.sourcePath], ["存储文件", data.storedPath], ["创建时间", data.createdAt], ["更新时间", data.updatedAt], ["文件大小", formatBytes(data.bytes)], ["分块", `${data.loadedChunks} / ${data.chunkCount}`]].map(([label, value]) => `<div class="source-row"><span>${esc(label)}</span><code>${esc(value || "--")}</code></div>`).join("");
    const warning = data.extractionWarning ? `<div class="knowledge-warning">${esc(data.extractionWarning)}</div>` : "";
    const isPdf = data.format === "pdf";
    const pdfUrl = `/api/knowledge/pdf?id=${encodeURIComponent(data.id)}`;
    const contentTab = isPdf ? `<div class="pdf-toolbar"><a class="secondary-button" href="${pdfUrl}" target="_blank" rel="noopener">在新窗口打开 PDF</a></div><iframe class="pdf-viewer" src="${pdfUrl}" title="PDF 预览"></iframe>` : `${warning}<div class="markdown">${renderMarkdown(data.content || "文档正文为空")}</div>${data.contentTruncated ? '<div class="empty-state">正文较长，当前只展示前 1.2 MB</div>' : ""}`;
    const html = `<div class="detail-pills">${(data.tags || []).map((tag) => `<span>${esc(tag)}</span>`).join("")}<span>${data.chunkCount || 0} CHUNKS</span><span>${formatBytes(data.bytes)}</span><span>${esc(String(data.contentSource || "stored").toUpperCase())}</span></div><div class="knowledge-tabs"><button class="active" data-knowledge-tab="content">${isPdf ? "PDF 原文" : "提取正文"}</button><button data-knowledge-tab="chunks">分块</button><button data-knowledge-tab="info">原文件与信息</button></div><section class="knowledge-panel" data-knowledge-panel="content">${contentTab}</section><section class="knowledge-panel" data-knowledge-panel="chunks" hidden>${chunks}${data.loadedChunks < data.chunkCount ? `<div class="empty-state">为保证页面流畅，仅展示前 ${data.loadedChunks} 个分块</div>` : ""}</section><section class="knowledge-panel" data-knowledge-panel="info" hidden><div class="source-table">${sourceRows}</div></section>`;
    openDrawer(data.title || documentId, "KNOWLEDGE DETAIL", html);
  } catch (error) { openDrawer("知识库读取失败", "ERROR", `<div class="empty-state">${esc(error.message)}</div>`); }
}

function connectEvents() {
  if (!("EventSource" in window)) return;
  const source = new EventSource("/api/events");
  source.addEventListener("dashboard", (event) => { try { render(JSON.parse(event.data)); } catch (error) { console.warn("Invalid dashboard event", error); } });
  source.onerror = () => { source.close(); setTimeout(connectEvents, 3000); };
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
  openDrawer("正在读取 Worker", "WORKER DETAIL", '<div class="empty-state">加载中…</div>');
  try {
    const data = await fetchJson(`/api/worker?id=${encodeURIComponent(workerId)}`);
    const artifacts = (data.runs || []).flatMap((run) => run.artifacts || []);
    const artifactHtml = artifacts.length ? artifacts.map((item) => `<div class="artifact"><div><strong>${esc(item.name)}</strong><small>${esc(item.path)} · ${formatBytes(item.bytes)}</small></div>${item.exists ? `<a href="/api/file?path=${encodeURIComponent(item.path)}">下载</a>` : '<span class="muted-tag">文件不存在</span>'}</div>`).join("") : '<div class="empty-state">没有登记产物</div>';
    const runsHtml = (data.runs || []).map((run, index) => `<details class="accordion" ${index === 0 ? "open" : ""}><summary><span>${esc(run.name)} · ${statusLabel(run.status)}</span><span>${esc(run.updatedAt)}</span></summary><div class="accordion-body">${run.report ? `<div class="markdown">${renderMarkdown(run.report)}</div>` : ""}<pre class="json-view">${esc(jsonText(run.result || run.progress || {}))}</pre></div></details>`).join("") || '<div class="empty-state">旧 Worker 没有独立运行目录，已使用 registry 内嵌结果。</div>';
    const html = `<div class="detail-pills"><span>${esc(data.status)}</span><span>${esc(data.capability || "general")}</span><span>${esc(data.model)}</span><span>run ${esc(data.runIndex)}</span><span>${esc(data.updatedAt)}</span></div><section class="drawer-section"><h3>任务</h3><p>${esc(data.task || data.topic)}</p></section><section class="drawer-section"><h3>最后回复</h3><div class="markdown">${renderMarkdown(data.report || data.summary || "暂无最终回复")}</div></section><section class="drawer-section"><h3>产物</h3>${artifactHtml}</section><section class="drawer-section"><h3>运行历史</h3>${runsHtml}</section><details class="accordion"><summary><span>原始结果</span><span>JSON</span></summary><div class="accordion-body"><pre class="json-view">${esc(jsonText(data.result || {}))}</pre></div></details>`;
    openDrawer(data.topic, "WORKER DETAIL", html);
  } catch (error) { openDrawer("Worker 读取失败", "ERROR", `<div class="empty-state">${esc(error.message)}</div>`); }
}

function memoryRecord(item) {
  if (!item || typeof item !== "object") return `<pre class="json-view">${esc(jsonText(item))}</pre>`;
  const title = item.project || item.fact || item.capability || item.lesson || item.name || item.title || "记忆条目";
  const description = item.description || item.snippet || item.trigger || item.status || "";
  const meta = [item.category, item.timestamp || item.last_seen || item.learned_at, item.confidence != null ? `置信度 ${item.confidence}` : ""].filter(Boolean).join(" · ");
  return `<div class="drawer-section"><h3>${esc(title)}</h3>${description ? `<p>${esc(description)}</p>` : ""}${meta ? `<div class="detail-pills"><span>${esc(meta)}</span></div>` : ""}</div>`;
}

function renderMemoryValue(value, depth = 0) {
  if (Array.isArray(value)) {
    const visible = value.slice(0, 80).map(memoryRecord).join("");
    return visible + (value.length > 80 ? `<div class="empty-state">另有 ${value.length - 80} 条未展开</div>` : "");
  }
  if (value && typeof value === "object") {
    return Object.entries(value).map(([key, child]) => {
      const count = Array.isArray(child) ? `${child.length} 条` : child && typeof child === "object" ? `${Object.keys(child).length} 项` : "1 项";
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
  select.innerHTML = items.length ? items.map((item) => `<option value="${esc(item.id)}">${esc(item.name)}${item.identity ? ` · ${esc(item.identity)}` : ""}</option>`).join("") : '<option value="">暂无记忆账号</option>';
  select.disabled = !items.length;
  select.value = current;
  if (current && current !== currentMemoryId) loadMemoryDetail(current);
  else if (!current) $("memory-page-body").innerHTML = '<div class="empty-state glass-card">暂无记忆档案 — 与 G4W 完成对话后会自动生成。</div>';
}

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
        ? `<span class="status-running">● 已安装（${esc(status.mode || "")}${status.enabled ? " · 已启用" : " · 未启用"}）</span>`
        : `<span class="status-stopped">○ 未安装（关键词检索可用）</span>`;
    }
    const network = (data.config || {}).network || "domestic";
    const net = $("embedding-network");
    if (net) net.checked = network !== "abroad";
    if (message) message.textContent = network === "domestic"
      ? "网络偏好：国内（镜像加速）。测速选优结果自动用于安装源。"
      : "网络偏好：国外（官方源）。测速选优结果自动用于安装源。";
  } catch (error) {
    if (message) message.textContent = `读取失败：${error.message}`;
  }
}

async function runEmbeddingSpeedtest() {
  const box = $("embedding-speedtest-result");
  const message = $("embedding-message");
  if (!box) return;
  box.innerHTML = '<div class="empty-state">正在测速（每源最多 4 秒）…</div>';
  try {
    const data = await fetchJson("/api/embedding/speedtest", { method: "POST" });
    if (!data.ok) {
      box.innerHTML = `<div class="empty-state">测速失败：${esc(data.error || "未知错误")}</div>`;
      if (message) message.textContent = "测速失败，请稍后重试。";
      return;
    }
    const rows = [];
    const kindLabel = { pip: "pip 源", torch: "torch 源", hf: "模型源" };
    for (const [kind, items] of Object.entries(data.results || {})) {
      rows.push(`<div class="speedtest-kind">${kindLabel[kind] || kind}${data.picked && data.picked[kind] ? ` · 已选：${esc(data.picked[kind])}` : ""}</div>`);
      (items || []).forEach((item) => {
        const best = data.picked && data.picked[kind] === item.url;
        const cls = best ? "speedtest-row best" : "speedtest-row";
        const latency = item.reachable ? `${item.latency_ms} ms` : "不可达";
        rows.push(`<div class="${cls}"><span>${esc(item.name)}</span><code>${esc(item.url)}</code><b>${latency}${best ? " ★" : ""}</b></div>`);
      });
    }
    box.innerHTML = rows.join("") || '<div class="empty-state">没有可测的镜像</div>';
    if (message) message.textContent = `测速完成${data.cached ? "（使用缓存）" : ""}：已自动选择延迟最低的源。`;
  } catch (error) {
    box.innerHTML = `<div class="empty-state">测速失败：${esc(error.message)}</div>`;
    if (message) message.textContent = "测速失败，请确认已登录并重试。";
  }
}

async function startEmbeddingInstall() {
  if (!confirm("开始安装向量检索环境？\n\n将下载约 4-8GB（torch + 模型），耗时较长。\n安装进度可在「服务与终端」页实时查看，期间可关闭本页面。")) return;
  const message = $("embedding-message");
  try {
    const data = await fetchJson("/api/embedding/install", { method: "POST" });
    if (!data.ok) {
      if (message) message.textContent = `启动失败：${data.error || "未知错误"}`;
      return;
    }
    if (message) message.textContent = `安装任务已启动（pid ${data.pid || "?"}）→ 点「安装日志」前往「服务与终端」页查看进度`;
    loadEmbeddingConfig(true);
  } catch (error) {
    if (message) message.textContent = `启动失败：${error.message}`;
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
    if (message) message.textContent = `已切换为：${network === "domestic" ? "国内（镜像加速）" : "国外（官方源）"}；下次安装/重装时生效。`;
  } catch (error) {
    if (message) message.textContent = `保存失败：${error.message}`;
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
    $("todo-page-list").innerHTML = `<div class="empty-state">读取待办失败：${esc(error.message)}</div>`;
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
      ? '<div class="empty-state">还没有已办任务</div>'
      : '<div class="empty-state">没有待办 🎉</div>';
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
        const { sender, id, act } = btn.dataset;
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
  if (info) info.textContent = `第 ${todoPage} / ${totalPages} 页 · 共 ${active.length} 条`;
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
  const labels = { overdue: "⚠️ 已到期", scheduled: "⏰ 有时间", ongoing: "📌 持续", done: "✅ 已完成" };
  const due = task.dueLabel ? ` <span class="muted">· ${esc(task.dueLabel)}</span>` : "";
  const recur = task.recurLabel ? ` <span class="muted">· ${esc(task.recurLabel)}</span>` : "";
  const fire = task.fireIndex > 0 ? ` <span class="muted">· 已提醒 ${task.fireIndex} 次</span>` : "";
  const del = `<button class="todo-act" data-todo-act="delete" data-sender="${esc(sender)}" data-id="${esc(task.id)}" title="删除">🗑</button>`;
  const act = task.status === "pending"
    ? `<button class="todo-act" data-todo-act="done" data-sender="${esc(sender)}" data-id="${esc(task.id)}" title="完成">✅</button>${del}`
    : del;
  return `<div class="todo-item todo-${state}"><span class="todo-badge">${labels[state] || ""}</span><span class="todo-text">${esc(task.text)}</span><span class="todo-meta">${due}${recur}${fire}</span><span class="todo-actions">${act}</span></div>`;
}

// ---- 服务与终端（服务启停 + 实时日志终端） ----
let servicesCache = null;
let servicesCacheAt = 0;
let servicesSignature = "";
let terminalId = "";
let terminalCursor = 0;
let terminalTimer = null;
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
    ? `<button class="service-act" data-service-start="${esc(svc.id)}" ${running ? "disabled" : ""}>启动</button><button class="service-act danger" data-service-stop="${esc(svc.id)}" ${running ? "" : "disabled"}>停止</button>`
    : '<span class="muted-tag">看板自身</span>';
  return `<div class="service-item" data-service-select="${esc(svc.id)}" title="点击查看日志">
    <div class="service-head">
      <span class="service-dot svc-${running ? "running" : "stopped"}"></span>
      <div class="service-info"><strong>${esc(svc.name)}</strong><small>${esc(svc.desc || svc.id)}</small></div>
      <span class="service-state ${running ? "is-running" : ""}">${running ? "运行中" : "已停止"}</span>
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
    listEl.innerHTML = '<div class="empty-state">暂无服务</div>';
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
          showToast(res.message || `已启动${res.pid ? `（pid ${res.pid}）` : ""}`);
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
          showToast(res.actions && res.actions.length ? `已停止（${res.actions.length} 项操作）` : "已停止");
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
      $("terminal-title").textContent = "选择一个服务";
      $("terminal-body").innerHTML = '<div class="empty-state">服务已移除，从左侧重新选择。</div>';
    } else {
      $("terminal-title").textContent = `${svc.name} · ${svc.status === "running" ? "运行中" : "已停止"}`;
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
  while (body.childElementCount > 2000) body.removeChild(body.firstChild); // 只保留最近 2000 行
  if (follow) body.scrollTop = body.scrollHeight;
}

function stopTerminalPoll() {
  if (terminalTimer) { clearInterval(terminalTimer); terminalTimer = null; }
}

function startTerminalPoll() {
  stopTerminalPoll();
  const tick = async () => {
    if (document.hidden) return; // 后台标签不轮询，减少连接占用
    if (!terminalId) return;
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
  if (body) body.innerHTML = `<div class="empty-state">正在连接 ${esc(sid)}…</div>`;
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
        appendWorkerMonitorLines([`── ${res.label || "worker"} 输出 ──`]);
      }
      if (typeof res.cursor === "number") workerMonitorCursor = res.cursor;
      if (res.lines && res.lines.length) appendWorkerMonitorLines(res.lines);
      if (res.progress && res.progress.key !== workerMonitorProgressKey) {
        workerMonitorProgressKey = res.progress.key;
        const summary = String(res.progress.summary || "").replace(/\s+/g, " ").slice(0, 120);
        appendWorkerMonitorLines([`[run] ${res.progress.label || ""} · turn ${res.progress.turn}：${summary}`]);
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
  appendWorkerMonitorLines([`── ${label || "worker"} 输出 ──`]);
  const allBtn = $("worker-monitor-all");
  if (allBtn) allBtn.hidden = false;
  const title = $("worker-monitor-title");
  if (title) title.textContent = `${label || "worker"} 输出`;
  if (document.querySelector('[data-page-view="workers"]')?.classList.contains("active")) startWorkerMonitorPoll();
}

function clearWorkerMonitorFilter() {
  stopWorkerMonitorPoll();
  workerMonitorWorkerDir = "";
  workerMonitorWorkerId = "";
  resetWorkerMonitorState();
  const body = $("worker-monitor-body");
  if (body) body.innerHTML = "";
  appendWorkerMonitorLines(["── 全部 worker 输出 ──"]);
  const allBtn = $("worker-monitor-all");
  if (allBtn) allBtn.hidden = true;
  const title = $("worker-monitor-title");
  if (title) title.textContent = "Worker 输出监视器";
  if (document.querySelector('[data-page-view="workers"]')?.classList.contains("active")) startWorkerMonitorPoll();
}

async function updateOverviewStartButton(force = false) {
  const btn = $("overview-start");
  if (!btn) return;
  const data = await loadServices(force);
  const main = (data.services || []).find((s) => s.id === "main");
  if (!main) {
    btn.textContent = "▶ 一键启动 G4W";
    btn.classList.remove("running");
    btn.disabled = true;
    return;
  }
  const running = main.status === "running";
  btn.textContent = running ? "● 运行中 · 查看日志" : "▶ 一键启动 G4W";
  btn.classList.toggle("running", running);
  btn.disabled = false;
}

async function startG4W() {
  const btn = $("overview-start");
  if (!btn || btn.disabled) return;
  if (btn.classList.contains("running")) { location.hash = "services"; routeTo("services"); return; }
  btn.disabled = true;
  btn.textContent = "正在启动…";
  try {
    const res = await fetchJson(`/api/services?action=start&id=main`);
    showToast(res.message || `已启动（pid ${res.pid || "?"}）`);
    // 与 start_G4W_ga.bat 行为一致：主服务拉起后附带 model monitor（失败不影响主服务）
    try { await fetchJson(`/api/services?action=start&id=monitor`); } catch (error) { /* 忽略 */ }
  } catch (error) {
    showToast(error.message, true);
    btn.textContent = "▶ 一键启动 G4W";
  }
  updateOverviewStartButton(true);
}

async function loadMemoryDetail(memoryId) {
  if (!memoryId) return;
  currentMemoryId = memoryId;
  $("memory-page-body").innerHTML = '<div class="empty-state glass-card">正在读取记忆…</div>';
  try {
    renderMemoryDetail(await fetchJson(`/api/memory?id=${encodeURIComponent(memoryId)}`));
  } catch (error) {
    $("memory-page-body").innerHTML = `<div class="empty-state glass-card">记忆读取失败：${esc(error.message)}</div>`;
  }
}

function renderMemoryDetail(data) {
  currentMemorySections = data.sections || [];
  currentMemoryData = data;
  const sections = currentMemorySections.map((section) => `<button class="memory-section-row" data-memory-section="${esc(section.key)}"><span>${esc(section.label)}</span><span class="count">${section.count} 条 · 查看 →</span></button>`).join("");
  // 版本历史:默认只显示最新 12 条,其余折叠展开
  const allBackups = data.backups || [];
  const backupItem = (item) => `<div class="artifact"><div><strong>${esc(item.name)}</strong><small>${esc(item.updatedAt)} · ${formatBytes(item.bytes)}</small></div><span class="muted-tag">历史快照</span></div>`;
  const backupsHtml = allBackups.length
    ? (backupsExpanded ? allBackups : allBackups.slice(0, 12)).map(backupItem).join("")
      + (allBackups.length > 12 ? `<button class="backup-expand" onclick="expandBackups(this)">${backupsExpanded ? "收起" : `展开全部 ${allBackups.length - 12} 条历史快照`} ${backupsExpanded ? "▴" : "▾"}</button>` : "")
    : '<div class="empty-state">没有历史版本</div>';
  const total = (data.sections || []).reduce((sum, section) => sum + (section.count || 0), 0);
  const identity = data.identity ? esc(data.identity) : "微信用户";
  const html = `<div class="memory-main">
      <article class="memory-profile-card glass-card">
        <div class="memory-person"><span class="memory-avatar">${esc((data.name || "记").slice(0, 1))}</span><div><h2>${esc(data.name)}</h2><small>${identity}</small></div></div>
        <div class="memory-profile-tags">${data.botName ? `<span class="muted-tag">助手 ${esc(data.botName)}</span>` : ""}<span class="muted-tag">更新 ${esc(data.updatedAt)}</span><span class="muted-tag">${formatBytes(data.activeBytes)}</span><span class="page-badge">${total} 条记忆</span></div>
      </article>
      <article class="glass-card memory-block"><div class="section-heading"><div><div class="eyebrow">USER PROFILE</div><h2>用户画像</h2></div><span class="muted-tag">总体稳定画像 · L4 持续维护</span></div><div class="markdown user-profile">${renderMarkdown(data.userProfile || "（暂无画像，等待 L4 生成）")}</div></article>
      <article class="glass-card memory-block"><div class="section-heading"><div><div class="eyebrow">MEMORY BRIEF</div><h2>记忆简报</h2></div></div><div class="markdown memory-brief">${renderMarkdown(data.brief || "暂无记忆简报")}</div></article>
      <article class="glass-card memory-block"><div class="section-heading"><div><div class="eyebrow">PROFILE TIMELINE</div><h2>画像时间轴</h2></div><span class="muted-tag">点击快照查看当时的画像与变化</span></div><div id="profile-timeline" class="profile-timeline"><div class="empty-state">正在读取历史快照…</div></div></article>
      <article class="glass-card memory-block"><div class="section-heading"><div><div class="eyebrow">STRUCTURED MEMORY</div><h2>结构化记忆</h2></div><span class="muted-tag">点击分类展开</span></div>${sections || '<div class="empty-state">还没有结构化记忆</div>'}</article>
    </div>
    <aside class="memory-side">
      <article class="context-card glass-card"><div class="eyebrow">MEMORY STATUS</div><h2>存储状态</h2><div class="context-row"><span>账号档案</span><strong>${memoryProfiles.length}</strong></div><div class="context-row"><span>历史版本</span><strong>${(data.backups || []).length}</strong></div><div class="context-row"><span>当前数据</span><strong>${formatBytes(data.activeBytes)}</strong></div><div class="context-row"><span>更新时间</span><strong>${esc(data.updatedAt)}</strong></div><div class="context-row"><span>来源</span><code>${esc(data.source)}</code></div></article>
      <section class="glass-card memory-block"><div class="section-heading"><div><div class="eyebrow">VERSION HISTORY</div><h2>版本历史</h2></div></div>${backupsHtml}</section>
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
    box.innerHTML = `<div class="empty-state">时间轴加载失败：${esc(error.message)}</div>`;
  });
}

function renderProfileTimeline(box, memoryId) {
  const points = timelinePoints;
  if (!points.length) {
    box.innerHTML = '<div class="empty-state">还没有历史快照 — L4 压缩后自动生成。</div>';
    return;
  }
  const first = points[0], last = points[points.length - 1];
  box.innerHTML = `<div class="timeline-range"><span>最新 ${esc(first.updatedAt.split(" ")[0])}</span><span>${points.length} 份快照</span><span>最早 ${esc(last.updatedAt.split(" ")[0])}</span></div><div class="timeline-track">${points.map((point) => {
    const profileCount = point.counts && point.counts.user_profile != null ? point.counts.user_profile : "-";
    return `<button class="tl-node" onclick="openBackupSnapshot('${esc(memoryId)}','${esc(point.name)}')" title="${esc(point.updatedAt)} · 画像 ${profileCount} 键 · ${formatBytes(point.bytes)}"><span class="tl-dot"></span><span class="tl-date">${esc(point.updatedAt.split(" ")[0])}</span><span class="tl-meta">${esc(point.updatedAt.split(" ")[1] || "")}</span></button>`;
  }).join("")}</div>`;
}

function openBackupSnapshot(memoryId, name) {
  openDrawer("正在读取快照…", "PROFILE SNAPSHOT", '<div class="empty-state">加载中…</div>');
  const index = timelinePoints.findIndex((point) => point.name === name);
  const prevName = index > 0 ? timelinePoints[index - 1].name : null;
  Promise.all([
    fetchJson(`/api/memory/backup?id=${encodeURIComponent(memoryId)}&name=${encodeURIComponent(name)}`),
    prevName ? fetchJson(`/api/memory/backup?id=${encodeURIComponent(memoryId)}&name=${encodeURIComponent(prevName)}`) : Promise.resolve(null),
  ]).then(([current, prev]) => {
    openDrawer(`${current.updatedAt} · 画像快照`, "PROFILE SNAPSHOT", renderBackupSnapshot(current, prev));
  }).catch((error) => {
    openDrawer("快照读取失败", "ERROR", `<div class="empty-state">${esc(error.message)}</div>`);
  });
}

const PROFILE_KEY_LABELS = { user_profile: "用户画像", ongoing_projects: "进行中项目", agent_capabilities_learned: "能力经验", memory_lessons: "记忆经验", user_facts: "用户事实", _meta: "元信息" };

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
  if (diff.added.length) parts.push(`<div class="snap-note add">新增 ${diff.added.length} 条</div>`);
  if (diff.removed.length) parts.push(`<div class="snap-note rem">移除 ${diff.removed.length} 条</div>`);
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
  const pills = [`<span>${esc(current.updatedAt)}</span>`, `<span>${formatBytes(JSON.stringify(curActive).length)}</span>`, `<span>${keys.length} 个分类</span>`];
  if (prev) pills.push(`<span>对比 ${esc(prev.updatedAt.split(" ")[0])}</span>`);
  const briefHtml = current.brief ? `<article class="snap-brief"><div class="eyebrow">当时简报</div><div class="markdown">${renderMarkdown(current.brief)}</div></article>` : "";
  return `<div class="detail-pills">${pills.join("")}</div>${briefHtml}<div class="snap-keys">${blocks}</div>`;
}

function selectPromptNode(node, button) {
  document.querySelectorAll(".tree-node").forEach((item) => item.classList.remove("active"));
  if (button) button.classList.add("active");
  $("prompt-node-meta").innerHTML = [`来源 ${node.source || "--"}`, `${node.chars || 0} chars`, `≈ ${node.tokens || 0} tokens`, `SHA ${node.sha || "--"}`, node.updatedAt || "--", node.inModel ? "进入模型" : "仅 UI"].map((value) => `<span>${esc(value)}</span>`).join("");
  $("prompt-node-content").textContent = node.content || "该节点没有可展示内容。";
}

function renderPromptTree(data) {
  const nodes = [...(data.components || []), data.thinkingNode].filter(Boolean);
  $("prompt-tree").innerHTML = nodes.map((node, index) => `<div class="tree-group"><button class="tree-node ${index === 0 ? "active" : ""}" data-prompt-node="${index}"><i>${node.kind === "generated" ? "◇" : node.kind === "code" ? "⌘" : "▧"}</i><span>${esc(node.title)}</span><small>${node.chars || 0}</small></button>${(node.children || []).length ? `<div class="tree-children">${node.children.map((child, childIndex) => `<button class="tree-node" data-prompt-node="${index}" data-prompt-child="${childIndex}"><i>└</i><span>${esc(child.title)}</span><small>${child.chars || 0}</small></button>`).join("")}</div>` : ""}</div>`).join("");
  if (nodes[0]) selectPromptNode(nodes[0], $("prompt-tree").querySelector(".tree-node"));
}

async function loadPrompt() {
  $("prompt-tree").innerHTML = '<div class="empty-state">正在读取最近一次真实模型请求…</div>';
  try {
    promptData = await fetchJson("/api/prompt");
    $("prompt-model").textContent = promptData.model || "--";
    $("prompt-time").textContent = promptData.generatedAt || "--";
    $("prompt-chars").textContent = String((promptData.final || "").length);
    $("prompt-source").textContent = promptData.snapshotPath || "尚无 Input 快照";
    $("prompt-final").textContent = promptData.final || "当前没有可用的真实 System Prompt 快照。请开启完整 Input 快照并完成一次对话。";
    $("prompt-dynamic").textContent = promptData.dynamic || "当前快照没有动态检索内容。";
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
    return `<option value="${esc(value)}">${esc(label)}${item.current && useIndex ? " · 当前" : ""}</option>`;
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
  $("model-control-status").textContent = `${models.length} 个可用 · 主进程在线`;
}

async function loadModels(force = false) {
  if (modelsLoading || (!force && $("setting-conductor-model").dataset.loaded === "true")) return;
  modelsLoading = true;
  $("model-control-status").textContent = "正在读取…";
  try {
    const data = await fetchJson("/api/models");
    applyModelData(data);
    $("setting-conductor-model").dataset.loaded = "true";
  } catch (error) {
    document.querySelectorAll("[data-model-target]").forEach((select) => { select.disabled = true; });
    $("model-control-status").textContent = "主进程离线";
    showToast(error.message, true);
  } finally { modelsLoading = false; }
}

async function switchModel(select) {
  const target = select.dataset.modelTarget;
  const previous = select.dataset.committedValue || "";
  const value = select.value;
  document.querySelectorAll("[data-model-target]").forEach((item) => { item.disabled = true; });
  $("model-control-status").textContent = "切换并等待确认…";
  try {
    const result = await fetchJson("/api/model", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target, value }) });
    showToast(result.message || "模型已切换");
    document.querySelectorAll("[data-model-target]").forEach((item) => { item.dataset.loaded = ""; });
    await refresh();
    await loadModels(true);
  } catch (error) {
    select.value = previous;
    showToast(error.message, true);
    $("model-control-status").textContent = "切换失败";
    document.querySelectorAll("[data-model-target]").forEach((item) => { item.disabled = false; });
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = {
    userName: $("setting-user-name").value.trim(), userIdentity: $("setting-user-identity").value.trim(), userGender: $("setting-user-gender").value, botName: $("setting-bot-name").value.trim(),
    turnEnabled: $("setting-turn").checked, workerTurnEnabled: $("setting-worker-turn").checked, inputCaptureEnabled: $("setting-input").checked, chunkChars: Number($("setting-chunk").value),
    workspaceRoot: $("setting-workspace").value.trim(),
    checkinEnabled: $("setting-checkin").checked, checkinMin: Number($("setting-checkin-min").value), checkinMax: Number($("setting-checkin-max").value), vectorEnabled: $("setting-vector").checked, webSearchEnabled: $("setting-web-search").checked
  };
  const button = $("settings-save");
  button.disabled = true; button.textContent = "保存中…";
  try {
    const result = await fetchJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    settingsDirty = false;
    $("settings-message").textContent = result.message;
    showToast(result.message);
    await refresh();
  } catch (error) { $("settings-message").textContent = error.message; showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = "保存设置"; }
}

$("refresh-button").addEventListener("click", refresh);
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
$("knowledge-search").addEventListener("input", (event) => { knowledgeQuery = event.target.value; renderKnowledgeCatalog(dashboardData?.knowledge?.items || []); });
$("knowledge-tag").addEventListener("change", (event) => { knowledgeTag = event.target.value; renderKnowledgeCatalog(dashboardData?.knowledge?.items || []); });
$("settings-form").addEventListener("submit", saveSettings);
$("settings-form").addEventListener("input", (event) => { if (event.target.closest("[data-model-target]")) return; settingsDirty = true; $("settings-message").textContent = "有未保存的修改"; });
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
  const worker = event.target.closest("[data-worker-id]"); if (worker) { openWorker(worker.dataset.workerId); return; }
  const memorySection = event.target.closest("[data-memory-section]");
  if (memorySection) { openMemorySection(memorySection.dataset.memorySection); return; }
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
    showToast("检测到看板已在其他标签页打开：每个标签页都占一条 SSE 长连接，多开会耗尽浏览器连接配额导致请求超时，建议只保留一个标签页", true);
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

