// G4W 看板国际化内核（壳层文案 + 时间/日期格式化）
// 依赖：先加载 zh-CN.js / zh-TW.js / en-US.js（三份语言包各自往 window.G4W_I18N 里塞一个语言对象）。
// 对外只暴露一个全局：window.G4W_I18N_API = { t, getLang, setLang, applyStatic, getTimezone, setTimezone,
//   fmtTime, fmtDate, fmtHM, fmtWeekday, fmtShortDate, fmtHourLabel, timezoneOptions, LANGS, langLabels }
// 设计约束：
//   1) 任何一步缺失都不许抛到 UI —— 语言包没加载、localStorage 被禁、时区名不合法（后端不校验 tzdata，
//      只做字符串形态校验，真正的时区合法性只有浏览器知道），全部静默回落到 zh-CN / 跟随系统；
//   2) 只服务「壳层」文案。日志正文、终端输出、模型输出、SOP/人设正文、业务数据一律不进语言包；
//   3) 语言与时区是「偏好」而不是「数据」，因此只存 localStorage（真实落库由 app.js 打 /api/settings）。
(function () {
  "use strict";

  var LANGS = ["zh-CN", "zh-TW", "en-US"];
  var FALLBACK_LANG = "zh-CN";
  var STORAGE_LANG = "g4w-ui-locale";
  var STORAGE_TZ = "g4w-ui-timezone";
  var LANG_LABELS = { "zh-CN": "简体中文", "zh-TW": "繁體中文", "en-US": "English" };
  var TZ_PRESETS = [
    "Asia/Shanghai", "Asia/Taipei", "Asia/Hong_Kong", "Asia/Tokyo", "Asia/Seoul", "Asia/Singapore",
    "UTC", "Europe/London", "Europe/Berlin", "Europe/Paris", "Europe/Moscow",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Sao_Paulo",
    "Australia/Sydney", "Pacific/Auckland"
  ];
  var WEEKDAY_FALLBACK = {
    "zh-CN": ["周日", "周一", "周二", "周三", "周四", "周五", "周六"],
    "zh-TW": ["週日", "週一", "週二", "週三", "週四", "週五", "週六"],
    "en-US": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
  };

  var currentLang = FALLBACK_LANG;
  var currentTimezone = "";
  var formatterCache = {};

  function packs() { return window.G4W_I18N || {}; }

  function store(key, value) {
    try { window.localStorage.setItem(key, value); } catch (error) { /* 隐私模式/被禁用时忽略 */ }
  }

  function load(key) {
    try { return window.localStorage.getItem(key); } catch (error) { return null; }
  }

  function normalizeLang(value) {
    var text = String(value || "").trim();
    if (LANGS.indexOf(text) >= 0) return text;
    var lower = text.toLowerCase();
    for (var i = 0; i < LANGS.length; i += 1) {
      if (LANGS[i].toLowerCase() === lower) return LANGS[i];
    }
    if (lower.indexOf("zh") === 0) return lower.indexOf("tw") >= 0 || lower.indexOf("hk") >= 0 ? "zh-TW" : "zh-CN";
    if (lower.indexOf("en") === 0) return "en-US";
    return FALLBACK_LANG;
  }

  function rawLookup(lang, key) {
    var pack = packs()[lang];
    if (!pack || !Object.prototype.hasOwnProperty.call(pack, key)) return undefined;
    return pack[key];
  }

  function fill(text, args) {
    if (!args || !args.length) return String(text);
    return String(text).replace(/\{(\d+)\}/g, function (match, index) {
      var value = args[Number(index)];
      return value === undefined || value === null ? match : String(value);
    });
  }

  // t(key, ...args)：当前语言 → zh-CN → 原样返回 key（调用方可用 key 自查缺漏，页面不应长期显示裸 key）
  function t(key) {
    var args = Array.prototype.slice.call(arguments, 1);
    var value = rawLookup(currentLang, key);
    if (value === undefined) value = rawLookup(FALLBACK_LANG, key);
    if (value === undefined) return String(key);
    return fill(value, args);
  }

  function getLang() { return currentLang; }

  function setLang(lang) {
    currentLang = normalizeLang(lang);
    formatterCache = {};
    try { document.documentElement.lang = currentLang; } catch (error) { /* 忽略 */ }
    store(STORAGE_LANG, currentLang);
    applyStatic(document);
    window.dispatchEvent(new CustomEvent("g4w:i18nchange", { detail: { lang: currentLang, timezone: currentTimezone } }));
    return currentLang;
  }

  function getTimezone() { return currentTimezone; }

  // 时区只在浏览器认账时才留下；其它一律回落成 ""（跟随系统），与改造前的行为一致
  function resolveTimezone(value) {
    var text = String(value === undefined || value === null ? "" : value).trim();
    return text && isValidTimeZone(text) ? text : "";
  }

  function setTimezone(timezone) {
    currentTimezone = resolveTimezone(timezone);
    formatterCache = {};
    store(STORAGE_TZ, currentTimezone);
    window.dispatchEvent(new CustomEvent("g4w:i18nchange", { detail: { lang: currentLang, timezone: currentTimezone } }));
    return currentTimezone;
  }

  function isValidTimeZone(timezone) {
    if (!timezone || typeof Intl === "undefined" || !Intl.DateTimeFormat) return false;
    try { new Intl.DateTimeFormat("en-US", { timeZone: timezone }).format(new Date()); return true; }
    catch (error) { return false; }
  }

  function applyStatic(root) {
    var scope = root || document;
    if (!scope || typeof scope.querySelectorAll !== "function") return;
    scope.querySelectorAll("[data-i18n]").forEach(function (node) {
      var key = node.getAttribute("data-i18n");
      if (key) node.textContent = t(key);
    });
    scope.querySelectorAll("[data-i18n-title]").forEach(function (node) {
      var key = node.getAttribute("data-i18n-title");
      if (key) node.setAttribute("title", t(key));
    });
    scope.querySelectorAll("[data-i18n-placeholder]").forEach(function (node) {
      var key = node.getAttribute("data-i18n-placeholder");
      if (key) node.setAttribute("placeholder", t(key));
    });
    scope.querySelectorAll("[data-i18n-aria]").forEach(function (node) {
      var key = node.getAttribute("data-i18n-aria");
      if (key) node.setAttribute("aria-label", t(key));
    });
  }

  function toDate(value) {
    if (value instanceof Date) return value;
    if (typeof value === "number" && isFinite(value)) return new Date(value);
    if (typeof value === "string" && value.trim()) {
      var text = value.trim();
      var parsed = new Date(/^\d+$/.test(text) ? Number(text) : text);
      if (!isNaN(parsed.getTime())) return parsed;
    }
    return new Date();
  }

  // 建 Formatter：先用当前语言 + 当前时区；抛 RangeError（浏览器不认这个时区）就丢掉 timeZone；仍失败再退回 zh-CN。
  // 全部失败返回 null，由调用方走 getHours()/getMonth() 的本地时间兜底（= 跟随系统，与改造前一致）。
  function buildFormatter(options) {
    if (typeof Intl === "undefined" || !Intl.DateTimeFormat) return null;
    var base = {};
    Object.keys(options).forEach(function (key) { base[key] = options[key]; });
    var withZone = {};
    Object.keys(base).forEach(function (key) { withZone[key] = base[key]; });
    if (currentTimezone) withZone.timeZone = currentTimezone;
    var attempts = currentTimezone ? [withZone, base] : [base];
    for (var i = 0; i < attempts.length; i += 1) {
      try { return new Intl.DateTimeFormat(currentLang, attempts[i]); } catch (error) { /* RangeError → 试下一个 */ }
    }
    try { return new Intl.DateTimeFormat(FALLBACK_LANG, base); } catch (error) { return null; }
  }

  function formatter(cacheKey, options) {
    var scoped = currentLang + "|" + currentTimezone + "|" + cacheKey;
    if (formatterCache[scoped]) return formatterCache[scoped];
    var instance = buildFormatter(options);
    formatterCache[scoped] = instance;
    return instance;
  }

  function pad(value) { return String(value).padStart(2, "0"); }

  // 14:05:03
  function fmtTime(value) {
    var instance = formatter("time", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    var date = toDate(value);
    if (instance) return instance.format(date);
    return pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
  }

  // 14:05
  function fmtHM(value) {
    var instance = formatter("hm", { hour: "2-digit", minute: "2-digit", hour12: false });
    var date = toDate(value);
    if (instance) return instance.format(date);
    return pad(date.getHours()) + ":" + pad(date.getMinutes());
  }

  // 2026-10-21（固定 YYYY-MM-DD 形状，跨语言一致，便于和日志/筛选对齐）
  function fmtDate(value) {
    var instance = formatter("date", { year: "numeric", month: "2-digit", day: "2-digit" });
    var date = toDate(value);
    if (!instance || typeof instance.formatToParts !== "function") {
      return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate());
    }
    var parts = instance.formatToParts(date);
    var map = {};
    parts.forEach(function (part) { if (part.type !== "literal") map[part.type] = part.value; });
    return [map.year, map.month, map.day].map(function (part) { return String(part == null ? "" : part).replace(/[^0-9]/g, ""); }).join("-");
  }

  // 周三 / Wednesday
  function fmtWeekday(value) {
    var instance = formatter("weekday", { weekday: "short" });
    var date = toDate(value);
    if (instance) return instance.format(date);
    var table = WEEKDAY_FALLBACK[currentLang] || WEEKDAY_FALLBACK[FALLBACK_LANG];
    return table[date.getDay()];
  }

  // 日期键（服务端给的 "2026-10-21"）用 UTC 正午渲染，避免跨时区时整列日期漂移一天
  function dateKeyToUtc(value) {
    var match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ""));
    if (!match) return toDate(value);
    return new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12, 0, 0));
  }

  // 10/21
  function fmtShortDate(value) {
    if (typeof Intl === "undefined" || !Intl.DateTimeFormat) {
      var date = dateKeyToUtc(value);
      return (date.getUTCMonth() + 1) + "/" + date.getUTCDate();
    }
    var instance;
    try { instance = new Intl.DateTimeFormat(currentLang, { month: "numeric", day: "numeric", timeZone: "UTC" }); }
    catch (error) { instance = new Intl.DateTimeFormat(FALLBACK_LANG, { month: "numeric", day: "numeric", timeZone: "UTC" }); }
    var target = dateKeyToUtc(value);
    return instance.format(target);
  }

  // 08:00（时间轴小时刻度；UTC 定点 + 24 小时制，不受当前时区影响）
  function fmtHourLabel(hour) {
    var date = new Date(Date.UTC(2000, 0, 1, Number(hour) || 0, 0, 0));
    if (typeof Intl !== "undefined" && Intl.DateTimeFormat) {
      try {
        return new Intl.DateTimeFormat(currentLang, { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" }).format(date);
      } catch (error) { /* 落到下面 */ }
    }
    return pad(Number(hour) || 0) + ":00";
  }

  function utcOffsetLabel(timezone) {
    try {
      var parts = new Intl.DateTimeFormat("en-US", { timeZone: timezone, timeZoneName: "shortOffset" }).formatToParts(new Date());
      var offset = parts.filter(function (part) { return part.type === "timeZoneName"; })[0];
      return offset ? offset.value : "";
    } catch (error) { return ""; }
  }

  // 下拉选项：优先浏览器自己确认的 IANA 列表（Intl.supportedValuesOf），没有该 API 时退回常用名；
  // 两种情况下都被 isValidTimeZone 过滤过 —— 后端没有 tzdata，非法值只能在这里挡住。
  function timezoneOptions() {
    var values = [];
    if (typeof Intl !== "undefined" && typeof Intl.supportedValuesOf === "function") {
      try { values = Intl.supportedValuesOf("timeZone") || []; } catch (error) { values = []; }
    }
    if (!values.length) values = TZ_PRESETS.filter(isValidTimeZone);
    TZ_PRESETS.forEach(function (zone) {
      if (values.indexOf(zone) < 0 && isValidTimeZone(zone)) values.push(zone);
    });
    var rows = [{ value: "", label: t("env.tz.followSystem") }];
    values.forEach(function (zone) {
      var offset = utcOffsetLabel(zone);
      rows.push({ value: zone, label: offset ? zone + " (" + offset + ")" : zone });
    });
    return rows;
  }

  var api = {
    LANGS: LANGS,
    langLabels: LANG_LABELS,
    t: t,
    getLang: getLang,
    setLang: setLang,
    getTimezone: getTimezone,
    setTimezone: setTimezone,
    resolveTimezone: resolveTimezone,
    applyStatic: applyStatic,
    fmtTime: fmtTime,
    fmtHM: fmtHM,
    fmtDate: fmtDate,
    fmtWeekday: fmtWeekday,
    fmtShortDate: fmtShortDate,
    fmtHourLabel: fmtHourLabel,
    timezoneOptions: timezoneOptions
  };

  window.G4W_I18N_API = api;

  // 首屏：localStorage（app.js 拿到 /api/dashboard 的 system.ui 后会覆盖成服务端值）
  currentLang = normalizeLang(load(STORAGE_LANG) || FALLBACK_LANG);
  currentTimezone = resolveTimezone(load(STORAGE_TZ) || "");
  try { document.documentElement.lang = currentLang; } catch (error) { /* 忽略 */ }

  function firstPaint() { applyStatic(document); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", firstPaint);
  else firstPaint();
})();
