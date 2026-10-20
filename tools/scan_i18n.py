#!/usr/bin/env python3
"""G4W Frontend i18n Scanner, Batch Replacer & Dictionary Generator.

Usage:
  python3 tools/scan_i18n.py           # Scan, update dictionaries, and apply i18n to HTML/JS
  python3 tools/scan_i18n.py --dry-run # Scan and report without modifying HTML/JS files
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "runtime" / "G4W-main" / "G4W" / "dashboard" / "static"
I18N_DIR = STATIC_DIR / "i18n"

CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# Regional & cultural terminology overrides for Traditional Chinese (zh-TW)
ZH_TW_EXACT_OVERRIDES = {
    "网络偏好：国内镜像加速": "網路偏好：中國大陸鏡像加速",
    "开=国内镜像（推荐） · 关=国外官方源": "開=中國大陸鏡像（推薦） · 關=國際官方源",
    "开=gh-proxy 镜像（推荐） · 关=直连 GitHub": "開=gh-proxy 鏡像（推薦） · 關=直連 GitHub",
    "国内（镜像加速）": "中國大陸（鏡像加速）",
    "国外（官方源）": "國際（官方源）",
    "网络偏好：国内（镜像加速）。测速选优结果自动用于安装源。": "網路偏好：中國大陸（鏡像加速）。測速選優結果自動用於安裝源。",
    "网络偏好：国外（官方源）。测速选优结果自动用于安装源。": "網路偏好：國際（官方源）。測速選優結果自動用於安裝源。",
    "国内网络请打开「国内镜像加速」，或为 G4W 进程设置 HTTPS_PROXY（例如 http://127.0.0.1:7897）。": "中國大陸網路請打開「中國大陸鏡像加速」，或為 G4W 進程設定 HTTPS_PROXY（例如 http://127.0.0.1:7897）。",
    "北京时间": "北京時間",
    "台北时间": "台北時間",
    "美国时间": "美國時間",
    "切换语言后立即翻译界面并自动匹配对应时区（简体中文→北京时间、繁體中文→台北時間、English→US Time），也可单独指定时区。": "切換語言後立即翻譯介面並自動匹配對應時區（簡體中文→北京時間、繁體中文→台北時間、English→US Time），也可單獨指定時區。",
}

EN_US_OVERRIDES = {
    "切换语言后立即翻译界面并自动匹配对应时区（简体中文→北京时间、繁體中文→台北時間、English→US Time），也可单独指定时区。": "Switching language immediately translates the UI and matches the default time zone (zh-CN → Beijing, zh-TW → Taipei, English → US Time), or you can select a specific time zone.",
    "北京时间": "Beijing Time (Asia/Shanghai)",
    "台北时间": "Taipei Time (Asia/Taipei)",
    "网络偏好：国内镜像加速": "Network Preference: China Mainland Mirror Acceleration",
    "开=国内镜像（推荐） · 关=国外官方源": "ON = China Mainland mirrors · OFF = International official sources",
    "开=gh-proxy 镜像（推荐） · 关=直连 GitHub": "ON = gh-proxy mirror (China Mainland) · OFF = Direct GitHub",
    "国内（镜像加速）": "China Mainland (Mirror Acceleration)",
    "国外（官方源）": "International (Official Sources)",
}


def transform_html(html_text: str, collected: set[str]) -> str:
    """Scan HTML outside <script>/<style>/<!-- --> and attach data-i18n attributes."""
    chunks = re.split(r"(<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->)", html_text)
    out = []
    for chunk in chunks:
        if chunk.startswith("<script"):
            # If it's an inline script with code, transform its JS body too
            m = re.match(r"^(<script[^>]*>)([\s\S]*?)(</script>)$", chunk)
            if m and m.group(2).strip():
                out.append(m.group(1) + transform_js(m.group(2), collected) + m.group(3))
            else:
                out.append(chunk)
            continue
        if chunk.startswith(("<style", "<!--")):
            out.append(chunk)
            continue

        # 1. Mixed <label>TEXT<input|select|textarea>
        def wrap_label_text(m):
            attrs = m.group(1) or ""
            txt = m.group(2)
            next_tag = m.group(3)
            lead = txt[:len(txt) - len(txt.lstrip())]
            trail = txt[len(txt.rstrip()):]
            core = txt.strip()
            collected.add(core)
            return f"<label{attrs}>{lead}<span data-i18n={json.dumps(core, ensure_ascii=False)}>{core}</span>{trail}{next_tag}"

        chunk = re.sub(
            r"<label(\s[^>]*)?>([^<]*[\u4e00-\u9fff][^<]*)(<(?:input|select|textarea)\b)",
            wrap_label_text,
            chunk,
        )

        # 2. Mixed .nav-item: <a class="nav-item..."><span>ICON</span>TEXT</a>
        def wrap_nav_text(m):
            prefix = m.group(1)
            txt = m.group(2).strip()
            suffix = m.group(3)
            collected.add(txt)
            return f'{prefix}<em data-i18n={json.dumps(txt, ensure_ascii=False)} style="font-style:normal">{txt}</em>{suffix}'

        chunk = re.sub(
            r'(<a\s+class="nav-item[^"]*"[^>]*><span>[^<]*</span>)([^<]*[\u4e00-\u9fff][^<]*)(</a>)',
            wrap_nav_text,
            chunk,
        )

        # 3. Mixed .step in wizard.html: <div class="step"...><b>...</b>TEXT</div>
        def wrap_step_text(m):
            prefix = m.group(1)
            txt = m.group(2).strip()
            suffix = m.group(3)
            collected.add(txt)
            return f"{prefix}<span data-i18n={json.dumps(txt, ensure_ascii=False)}>{txt}</span>{suffix}"

        chunk = re.sub(
            r'(<div class="step"[^>]*><b[^>]*>[^<]*</b>)([^<]*[\u4e00-\u9fff][^<]*)(</div>)',
            wrap_step_text,
            chunk,
        )

        # 4. Leaf tags: <tag attrs>TEXT</tag> (skip <option> inside #setting-ui-lang / #wizard-lang-select if fixed language names)
        def add_attr_to_leaf_tag(m):
            tag = m.group(1)
            attrs = m.group(2) or ""
            txt = m.group(3)
            close = m.group(4)
            core = txt.strip()
            if not core:
                return m.group(0)
            # Do not translate the language selector's native language names
            if tag == "option" and any(v in attrs for v in ('value="zh-CN"', 'value="zh-TW"', 'value="en-US"')):
                return m.group(0)
            collected.add(core)
            if "data-i18n=" in attrs:
                return m.group(0)
            return f"<{tag}{attrs} data-i18n={json.dumps(core, ensure_ascii=False)}>{txt}{close}"

        chunk = re.sub(
            r"<([a-zA-Z0-9]+)(\s[^>]*)?>([^<>]*[\u4e00-\u9fff][^<>]*)(</\1>)",
            add_attr_to_leaf_tag,
            chunk,
        )

        # 5. Any remaining bare CJK text nodes between tags (e.g. mixed inline code/b)
        def wrap_remaining_text(m):
            txt = m.group(1)
            lead = txt[:len(txt) - len(txt.lstrip())]
            trail = txt[len(txt.rstrip()):]
            core = txt.strip()
            if not core:
                return m.group(0)
            collected.add(core)
            return f">{lead}<span data-i18n={json.dumps(core, ensure_ascii=False)}>{core}</span>{trail}<"

        # Only wrap if not already inside a tag with data-i18n
        def check_and_wrap_remaining( chunk_str ):
            res = []
            last = 0
            for m in re.finditer(r">([^<>]*[\u4e00-\u9fff][^<>]*)<", chunk_str):
                preceding = chunk_str[max(0, m.start() - 500):m.start() + 1]
                # Check if the opening tag right before > has data-i18n= or is a language option
                open_tag = preceding.rsplit("<", 1)[-1] if "<" in preceding else ""
                if "data-i18n=" in open_tag or ('option' in open_tag and any(v in open_tag for v in ('value="zh-CN"', 'value="zh-TW"', 'value="en-US"'))):
                    continue
                res.append(chunk_str[last:m.start()])
                res.append(wrap_remaining_text(m))
                last = m.end()
            res.append(chunk_str[last:])
            return "".join(res)

        chunk = check_and_wrap_remaining(chunk)

        # 6. Attributes: placeholder, title, aria-label (check tag-level to avoid duplicate data-i18n-* attrs)
        for attr in ("placeholder", "title", "aria-label"):
            data_attr = f"data-i18n-{attr}"
            def repl_tag_attr(m, attr=attr, data_attr=data_attr):
                tag_str = m.group(0)
                if f"{data_attr}=" in tag_str:
                    return tag_str
                def repl_a(am):
                    val = am.group(1)
                    collected.add(val.strip())
                    return f'{attr}="{val}" {data_attr}={json.dumps(val.strip(), ensure_ascii=False)}'
                return re.sub(rf'(?<!data-i18n-)\b{attr}="([^"]*[\u4e00-\u9fff][^"]*)"', repl_a, tag_str)
            chunk = re.sub(r"<[a-zA-Z0-9]+\s[^>]*>", repl_tag_attr, chunk)

        # Also collect already-annotated data-i18n attributes
        for m in re.finditer(r'\bdata-i18n(?:-(?:placeholder|title|aria-label))?="([^"]+)"', chunk):
            unesc = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
            collected.add(unesc)

        out.append(chunk)
    return "".join(out)


def is_already_wrapped_in_t(js: str, pos: int) -> bool:
    """Check if position `pos` in `js` is immediately preceded by `t(`."""
    k = pos - 1
    while k >= 0 and js[k] in (" ", "\t", "\r", "\n"):
        k -= 1
    if k < 1 or js[k] != "(":
        return False
    k -= 1
    while k >= 0 and js[k] in (" ", "\t", "\r", "\n"):
        k -= 1
    if k < 0 or js[k] != "t":
        return False
    if k > 0 and (js[k - 1].isalnum() or js[k - 1] in ("_", "$", ".")):
        return False
    return True


def transform_js(js: str, collected: set[str]) -> str:
    """Scan JS code and wrap CJK string/template literals in t(...) calls idempotently."""
    out = []
    i, n = 0, len(js)

    def unescape_js_str(raw: str) -> str:
        return (
            raw.replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\'", "'")
            .replace("\\\\", "\\")
        )

    def scan_expr() -> str:
        nonlocal i
        depth = 1
        expr_out = []
        last = i
        while i < n and depth > 0:
            ch = js[i]
            if ch == "{":
                depth += 1
                i += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    expr_out.append(js[last:i])
                    i += 1
                    break
                i += 1
            elif ch in ('"', "'", "`"):
                expr_out.append(js[last:i])
                already = is_already_wrapped_in_t(js, i)
                expr_out.append(scan_string(ch, already))
                last = i
            elif ch == "/" and i + 1 < n and js[i + 1] == "/":
                while i < n and js[i] != "\n":
                    i += 1
            elif ch == "/" and i + 1 < n and js[i + 1] == "*":
                i += 2
                while i + 1 < n and not (js[i] == "*" and js[i + 1] == "/"):
                    i += 1
                i += 2
            else:
                i += 1
        return "".join(expr_out)

    def replace_html_combined(combined: str, exprs: list[str]) -> str:
        def repl_attr(m):
            attr_name = m.group(1)
            raw_val = m.group(2)
            # If raw_val is purely a placeholder \x00idx\x00, leave it alone
            if re.fullmatch(r"\s*\x00\d+\x00\s*", raw_val):
                return m.group(0)
            args = []
            def sub_ph(pm):
                idx = int(pm.group(1))
                args.append(exprs[idx])
                return f"{{{len(args) - 1}}}"
            norm = re.sub(r"\x00(\d+)\x00", sub_ph, raw_val.strip())
            collected.add(unescape_js_str(norm))
            arg_str = (", " + ", ".join(args)) if args else ""
            return f'{attr_name}="${{t({json.dumps(unescape_js_str(norm), ensure_ascii=False)}{arg_str})}}"'

        combined = re.sub(r'(title|placeholder|aria-label)="([^"]*[\u4e00-\u9fff][^"]*)"', repl_attr, combined)

        def repl_node(m):
            prefix = m.group(1)
            raw_val = m.group(2)
            lead = raw_val[:len(raw_val) - len(raw_val.lstrip())]
            trail = raw_val[len(raw_val.rstrip()):]
            core = raw_val.strip()
            if not core or not CJK_RE.search(core):
                return m.group(0)
            if re.fullmatch(r"\x00\d+\x00", core):
                return m.group(0)
            args = []
            def sub_ph(pm):
                idx = int(pm.group(1))
                args.append(exprs[idx])
                return f"{{{len(args) - 1}}}"
            norm = re.sub(r"\x00(\d+)\x00", sub_ph, core)
            collected.add(unescape_js_str(norm))
            arg_str = (", " + ", ".join(args)) if args else ""
            return f"{prefix}{lead}${{t({json.dumps(unescape_js_str(norm), ensure_ascii=False)}{arg_str})}}{trail}"

        combined = re.sub(r"(^|>)([^<>]*[\u4e00-\u9fff][^<>]*)(?=<|$)", repl_node, combined)
        combined = re.sub(r"\x00(\d+)\x00", lambda m: f"${{{exprs[int(m.group(1))]}}}", combined)
        return "`" + combined + "`"

    def scan_string(quote: str, already_wrapped: bool = False) -> str:
        nonlocal i
        start = i
        i += 1
        if quote in ('"', "'"):
            while i < n:
                c = js[i]
                if c == "\\":
                    i += 2
                    continue
                if c == quote or c == "\n":
                    if c == quote:
                        i += 1
                    break
                i += 1
            lit = js[start:i]
            raw = lit[1:-1]
            if not CJK_RE.search(raw):
                return lit
            if "<" in raw and ">" in raw and not already_wrapped:
                return replace_html_combined(raw, [])
            collected.add(unescape_js_str(raw))
            if already_wrapped:
                return lit
            return f"t({lit})"
        elif quote == "`":
            parts, exprs, cur_start = [], [], i
            while i < n:
                c = js[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "$" and i + 1 < n and js[i + 1] == "{":
                    parts.append(js[cur_start:i])
                    i += 2
                    exprs.append(scan_expr())
                    cur_start = i
                    continue
                if c == "`":
                    parts.append(js[cur_start:i])
                    i += 1
                    break
                i += 1
            if not any(CJK_RE.search(p) for p in parts):
                return "`" + "".join(f"{p}${{{e}}}" for p, e in zip(parts[:-1], exprs)) + parts[-1] + "`"
            combined = "".join(f"{p}\x00{idx}\x00" for idx, p in enumerate(parts[:-1])) + parts[-1]
            if "<" in combined and ">" in combined and not already_wrapped:
                return replace_html_combined(combined, exprs)
            args = []
            def sub_ph(pm):
                idx = int(pm.group(1))
                args.append(exprs[idx])
                return f"{{{len(args) - 1}}}"
            norm = re.sub(r"\x00(\d+)\x00", sub_ph, combined)
            collected.add(unescape_js_str(norm))
            if already_wrapped:
                return "`" + "".join(f"{p}${{{e}}}" for p, e in zip(parts[:-1], exprs)) + parts[-1] + "`"
            arg_str = (", " + ", ".join(args)) if args else ""
            return f"t({json.dumps(unescape_js_str(norm), ensure_ascii=False)}{arg_str})"
        return js[start:i]

    last = 0
    while i < n:
        ch = js[i]
        if ch in ('"', "'", "`"):
            out.append(js[last:i])
            already = is_already_wrapped_in_t(js, i)
            out.append(scan_string(ch, already))
            last = i
        elif ch == "/" and i + 1 < n and js[i + 1] == "/":
            while i < n and js[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and js[i + 1] == "*":
            i += 2
            while i + 1 < n and not (js[i] == "*" and js[i + 1] == "/"):
                i += 1
            i += 2
        else:
            i += 1
    out.append(js[last:])
    return "".join(out)


def load_json_dict(path: pathlib.Path) -> dict[str, str]:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def build_locales_js(zh_cn: dict, zh_tw: dict, en_us: dict) -> str:
    return (
        "// Auto-generated by tools/scan_i18n.py — G4W Frontend i18n Runtime & Dictionaries\n"
        "window.G4W_LOCALES = {\n"
        '  "zh-CN": ' + json.dumps(zh_cn, ensure_ascii=False, indent=2) + ",\n"
        '  "zh-TW": ' + json.dumps(zh_tw, ensure_ascii=False, indent=2) + ",\n"
        '  "en-US": ' + json.dumps(en_us, ensure_ascii=False, indent=2) + "\n"
        "};\n\n"
        """(function () {
  const DEFAULT_TIMEZONES = {
    "zh-CN": "Asia/Shanghai",
    "zh-TW": "Asia/Taipei",
    "en-US": "America/New_York"
  };

  function normalizeLang(lang) {
    const raw = String(lang || "").trim();
    if (raw === "zh-TW" || raw === "zh-HK" || raw === "zh-Hant") return "zh-TW";
    if (raw === "en-US" || raw === "en" || raw.startsWith("en-")) return "en-US";
    return "zh-CN";
  }

  let currentLang = normalizeLang(localStorage.getItem("g4w-lang") || document.documentElement.lang || "zh-CN");
  let currentTzPref = localStorage.getItem("g4w-timezone") || "auto";

  function getLang() {
    return currentLang;
  }

  function getTimezonePref() {
    return currentTzPref;
  }

  function getTimezone() {
    if (currentTzPref && currentTzPref !== "auto") return currentTzPref;
    return DEFAULT_TIMEZONES[currentLang] || "Asia/Shanghai";
  }

  function getTimezoneDisplayLabel() {
    const tz = getTimezone();
    if (tz === "Asia/Taipei") return t("台北时间");
    if (tz === "America/New_York") return t("美东时间 (America/New_York)");
    if (tz === "America/Los_Angeles") return t("美西时间 (America/Los_Angeles)");
    if (tz === "UTC") return t("世界协调时间 (UTC)");
    if (tz === "Asia/Shanghai") {
      return currentLang === "zh-TW" ? "北京時間 (Asia/Shanghai)" : currentLang === "en-US" ? "Beijing Time (Asia/Shanghai)" : "北京时间";
    }
    return tz;
  }

  const REVERSE_KEY_MAP = {};
  if (window.G4W_LOCALES) {
    for (const lang of ["zh-TW", "en-US"]) {
      const d = window.G4W_LOCALES[lang] || {};
      for (const [k, v] of Object.entries(d)) {
        if (v && v !== k && REVERSE_KEY_MAP[v] === undefined) {
          REVERSE_KEY_MAP[v] = k;
        }
      }
    }
  }

  function t(key, ...args) {
    if (key == null) return "";
    const rawKey = String(key);
    const dict = (window.G4W_LOCALES && window.G4W_LOCALES[currentLang]) || {};
    let strKey = dict[rawKey] !== undefined ? rawKey : (REVERSE_KEY_MAP[rawKey] || rawKey);
    let val = dict[strKey] !== undefined ? dict[strKey] : rawKey;
    if (args.length > 0) {
      val = val.replace(/\\{(\\d+)\\}/g, (match, idx) => {
        const i = Number(idx);
        return args[i] !== undefined && args[i] !== null ? String(args[i]) : match;
      });
    }
    return val;
  }

  function formatTime(dateOrTs, options) {
    const d = dateOrTs instanceof Date ? dateOrTs : new Date(dateOrTs ?? Date.now());
    const tz = getTimezone();
    const opts = Object.assign({ hour12: false, timeZone: tz }, options || {});
    try {
      return d.toLocaleTimeString(currentLang, opts);
    } catch (_) {
      return d.toLocaleTimeString(currentLang, { hour12: false });
    }
  }

  function formatDate(dateOrTs, options) {
    const d = dateOrTs instanceof Date ? dateOrTs : new Date(dateOrTs ?? Date.now());
    const tz = getTimezone();
    const opts = Object.assign({ timeZone: tz }, options || {});
    try {
      return d.toLocaleDateString(currentLang, opts);
    } catch (_) {
      return d.toLocaleDateString(currentLang, options || {});
    }
  }

  function applyDomTranslations(root) {
    const scope = root || document;
    if (scope === document && document.documentElement) {
      document.documentElement.lang = currentLang;
    }
    scope.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      if (key) el.textContent = t(key);
    });
    ["placeholder", "title", "aria-label"].forEach((attr) => {
      const dataAttr = "data-i18n-" + attr;
      scope.querySelectorAll("[" + dataAttr + "]").forEach((el) => {
        const key = el.getAttribute(dataAttr);
        if (key) el.setAttribute(attr, t(key));
      });
    });
    const tzLabelEl = document.getElementById("timeline-tz-label");
    if (tzLabelEl) {
      tzLabelEl.textContent = getTimezoneDisplayLabel();
    }
    const langSelect = document.getElementById("setting-ui-lang");
    if (langSelect && langSelect.value !== currentLang) {
      langSelect.value = currentLang;
    }
    const tzSelect = document.getElementById("setting-ui-timezone");
    if (tzSelect && tzSelect.value !== currentTzPref) {
      tzSelect.value = currentTzPref;
    }
  }

  function setLang(lang, tzPref) {
    currentLang = normalizeLang(lang);
    localStorage.setItem("g4w-lang", currentLang);
    if (tzPref !== undefined) {
      currentTzPref = tzPref || "auto";
      localStorage.setItem("g4w-timezone", currentTzPref);
    }
    applyDomTranslations(document);
    window.dispatchEvent(new CustomEvent("g4w:i18n-changed", {
      detail: { lang: currentLang, timezone: getTimezone(), timezonePref: currentTzPref }
    }));
  }

  function setTimezonePref(tzPref) {
    currentTzPref = tzPref || "auto";
    localStorage.setItem("g4w-timezone", currentTzPref);
    applyDomTranslations(document);
    window.dispatchEvent(new CustomEvent("g4w:i18n-changed", {
      detail: { lang: currentLang, timezone: getTimezone(), timezonePref: currentTzPref }
    }));
  }

  window.t = t;
  window.g4wI18n = {
    t,
    getLang,
    setLang,
    getTimezone,
    getTimezonePref,
    setTimezonePref,
    getTimezoneDisplayLabel,
    formatTime,
    formatDate,
    applyDomTranslations,
    DEFAULT_TIMEZONES
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => applyDomTranslations(document));
  } else {
    applyDomTranslations(document);
  }
})();
"""
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan G4W frontend for CJK strings and apply i18n.")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, do not write modified files")
    args = parser.parse_args()

    I18N_DIR.mkdir(parents=True, exist_ok=True)
    zh_cn = load_json_dict(I18N_DIR / "zh-CN.json")
    zh_tw = load_json_dict(I18N_DIR / "zh-TW.json")
    en_us = load_json_dict(I18N_DIR / "en-US.json")

    collected: set[str] = set()

    html_files = [STATIC_DIR / "index.html", STATIC_DIR / "wizard.html"]
    js_files = [STATIC_DIR / "app.js"]

    modified_files: dict[pathlib.Path, str] = {}

    for hpath in html_files:
        if hpath.is_file():
            orig = hpath.read_text(encoding="utf-8")
            new_html = transform_html(orig, collected)
            modified_files[hpath] = new_html

    for jpath in js_files:
        if jpath.is_file():
            orig = jpath.read_text(encoding="utf-8")
            new_js = transform_js(orig, collected)
            modified_files[jpath] = new_js

    # Merge collected keys into dictionaries
    new_keys = 0
    for key in sorted(collected):
        if key not in zh_cn:
            zh_cn[key] = key
            new_keys += 1
        if key not in zh_tw:
            zh_tw[key] = ZH_TW_EXACT_OVERRIDES.get(key, key)
        if key not in en_us:
            en_us[key] = EN_US_OVERRIDES.get(key, key)

    # Apply overrides to existing dictionary entries
    for k, v in ZH_TW_EXACT_OVERRIDES.items():
        if k in zh_cn or k in collected:
            zh_cn[k] = k
            zh_tw[k] = v
    for k, v in EN_US_OVERRIDES.items():
        if k in zh_cn or k in collected:
            zh_cn[k] = k
            en_us[k] = v

    # Sort dictionaries by key
    zh_cn_sorted = dict(sorted(zh_cn.items()))
    zh_tw_sorted = {k: zh_tw.get(k, k) for k in zh_cn_sorted}
    en_us_sorted = {k: en_us.get(k, k) for k in zh_cn_sorted}

    print(f"[scan_i18n] Scanned {len(html_files)} HTML files and {len(js_files)} JS files.")
    print(f"[scan_i18n] Extracted {len(collected)} active frontend phrases ({len(zh_cn_sorted)} total dictionary entries, {new_keys} newly added).")

    if args.dry_run:
        print("[scan_i18n] Dry-run mode: no files modified.")
        return 0

    for fpath, content in modified_files.items():
        fpath.write_text(content, encoding="utf-8")
        print(f"[scan_i18n] Updated {fpath.relative_to(ROOT)}")

    (I18N_DIR / "zh-CN.json").write_text(json.dumps(zh_cn_sorted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (I18N_DIR / "zh-TW.json").write_text(json.dumps(zh_tw_sorted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (I18N_DIR / "en-US.json").write_text(json.dumps(en_us_sorted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (I18N_DIR / "locales.js").write_text(build_locales_js(zh_cn_sorted, zh_tw_sorted, en_us_sorted), encoding="utf-8")
    print(f"[scan_i18n] Updated dictionaries in {I18N_DIR.relative_to(ROOT)}/ (zh-CN.json, zh-TW.json, en-US.json, locales.js)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
