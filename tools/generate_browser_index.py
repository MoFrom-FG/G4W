#!/usr/bin/env python3
"""
B0 浏览层只读索引生成器 (Read-only Browser Index Generator)

Reads the short-path mirror tree (m/{c|r}/{id[0:2]}/{id[2:4]}/{id}.json)
and generates an immutable three-artifact generation plus a stable entry:
  - index.html    : Local browsable HTML with strict CSP, no external deps
  - messages.csv : Compact CSV with formula injection protection
  - index.json    : Full structured JSON for programmatic consumption
  - <output>/index.html: Atomically refreshed, self-contained file:// entry

Security guarantees:
  - DOM population uses textContent exclusively (never innerHTML)
  - Data embedding uses JSON serialization
  - CSP forbids external origins, eval, inline scripts
  - CSV fields starting with = + - @ are prefixed with ' (OWASP formula injection)
  - Paths are resolved and constrained to mirror root
  - Symlinks are not followed
  - Output is written to temp dir then atomically replaced
  - Preview is truncated to limited length
  - Sender IDs are sanitized for display (masked)

Usage:
  python generate_browser_index.py <mirror_root> [output_dir]

  mirror_root: Path to the short-path-mirror root (containing m/ directory)
  output_dir:  Where to write index.html, messages.csv, index.json (default: mirror_root/browse/)
"""

import json
import os
import sys
import hashlib
import base64
import tempfile
import shutil
import uuid
import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ── Security Constants ────────────────────────────────────────────────
PREVIEW_MAX_CHARS = 200          # Maximum chars for content preview
SENDER_MASK_KEEP_PREFIX = 3      # Keep first N chars of sender_id, mask rest
SENDER_MASK_CHAR = '*'
CSV_FORMULA_TRIGGER_CHARS = set('=+-@')

# Strict CSP: only the exact generated inline application script may execute.
# External scripts and eval remain forbidden because no origin, unsafe-inline,
# unsafe-eval, or strict-dynamic source is granted.
CSP_POLICY = (
    "default-src 'none'; "
    "script-src '__B0_SCRIPT_HASH__'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "connect-src 'none'; "
    "font-src 'none'; "
    "frame-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "require-trusted-types-for 'script'"
)

MIRROR_KIND_MAP = {'c': 'conversation', 'r': 'round'}

# Browse UI timezone: China Standard Time (UTC+8). Mirror JSON stays UTC.
DISPLAY_TZ = datetime.timezone(datetime.timedelta(hours=8))
DISPLAY_TZ_LABEL = 'UTC+8'


def format_display_time(value) -> str:
    """Convert stored UTC/offset timestamps to wall time for browse (UTC+8).

    Storage remains UTC (…Z). Display uses explicit +08:00 so local users
    match wall clock without mental conversion. Unparseable values pass through.
    """
    if value is None:
        return ''
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return ''
        # Common G4W form: 2026-07-26T11:20:01.176491Z
        if s.endswith('Z') or s.endswith('z'):
            s = s[:-1] + '+00:00'
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError:
            return str(value).strip()
    if dt.tzinfo is None:
        # Naive → treat as UTC (mirror writes UTC without local ambiguity)
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    local = dt.astimezone(DISPLAY_TZ)
    # Drop sub-second noise for table readability; keep offset explicit
    if local.microsecond:
        return local.strftime('%Y-%m-%dT%H:%M:%S') + f'.{local.microsecond:06d}+08:00'
    return local.strftime('%Y-%m-%dT%H:%M:%S') + '+08:00'


# ── Security Utilities ─────────────────────────────────────────────────

def sanitize_sender(sender_id: str) -> str:
    """Mask sender_id for display: keep prefix, mask the rest."""
    if not sender_id:
        return "(unknown)"
    if len(sender_id) <= SENDER_MASK_KEEP_PREFIX:
        return sender_id
    return sender_id[:SENDER_MASK_KEEP_PREFIX] + SENDER_MASK_CHAR * (len(sender_id) - SENDER_MASK_KEEP_PREFIX)


def truncate_preview(text: str, max_chars: int = PREVIEW_MAX_CHARS) -> str:
    """Truncate text to max_chars for preview, adding ellipsis if truncated."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def csv_escape_field(value: str) -> str:
    """
    Escape a CSV field with formula injection protection.
    
    Per OWASP: prefix fields starting with =, +, -, @ with a single quote
    to prevent spreadsheet formula injection.
    Also handles quoting for fields containing commas, quotes, or newlines.
    """
    if value is None:
        return ""
    
    s = str(value)
    
    # Formula injection protection: prefix trigger chars with '
    if s and s[0] in CSV_FORMULA_TRIGGER_CHARS:
        s = "'" + s
    
    # CSV quoting: if contains comma, double-quote, or newline, wrap in quotes
    if ',' in s or '"' in s or '\n' in s or '\r' in s:
        s = '"' + s.replace('"', '""') + '"'
    
    return s


def is_safe_path(base_dir: str, target_path: str) -> bool:
    """
    Verify that target_path resolves within base_dir, without symlink following.
    
    Uses os.path.realpath (resolves symlinks, which we explicitly deny)
    combined with os.path.abspath for path normalization.
    """
    try:
        # Resolve without following symlinks: first normalize, then check
        base_real = os.path.realpath(os.path.abspath(base_dir))
        target_abs = os.path.abspath(os.path.join(base_dir, target_path))
        
        # Check if target exists and is a symlink
        if os.path.islink(target_abs):
            return False  # Do not follow symlinks
        
        target_real = os.path.realpath(target_abs)
        
        # Must be under base_dir
        common = os.path.commonpath([base_real, target_real])
        return common == base_real
    except (ValueError, OSError):
        return False


def verify_mirror_root(mirror_root: str) -> str:
    """Verify and normalize a prototype ``m/`` or deployed ``c/``+``r/`` root."""
    if not os.path.isdir(mirror_root):
        raise ValueError(f"Mirror root does not exist or is not a directory: {mirror_root}")

    real_path = os.path.realpath(mirror_root)
    m_dir = os.path.join(real_path, 'm')
    direct_dirs = [os.path.join(real_path, branch) for branch in ('c', 'r')]
    if not os.path.isdir(m_dir) and not all(os.path.isdir(p) for p in direct_dirs):
        raise ValueError(
            f"No supported mirror layout (m/ or c/+r/) found in mirror root: {real_path}"
        )

    return real_path


# ── Mirror Reading ─────────────────────────────────────────────────────

def read_mirror_records(mirror_root: str) -> List[Dict]:
    """
    Walk the mirror tree and read all valid JSON records.
    
    Returns a list of record dicts with additional computed fields:
      - _relative_path: path relative to mirror_root
      - _read_error: error message if reading/parsing failed
    """
    records = []
    m_dir = os.path.join(mirror_root, 'm')
    scan_root = m_dir if os.path.isdir(m_dir) else mirror_root

    for dirpath, dirnames, filenames in os.walk(scan_root):
        # In the deployed direct layout, browse is derived output, never input.
        # Restrict only the root level; retain shard directories below c/ and r/.
        if scan_root == mirror_root and os.path.normcase(dirpath) == os.path.normcase(mirror_root):
            dirnames[:] = [d for d in dirnames if d in ('c', 'r')]
        # Sort for deterministic output
        dirnames.sort()
        filenames.sort()
        
        for fname in filenames:
            if not fname.endswith('.json'):
                continue
            
            fpath = os.path.join(dirpath, fname)
            
            # Refuse to follow symlinks
            if os.path.islink(fpath):
                records.append({
                    '_relative_path': os.path.relpath(fpath, mirror_root),
                    '_read_error': 'symlink_refused',
                    '_file_path': fpath,
                })
                continue
            
            # Get relative path for output
            rel_path = os.path.relpath(fpath, mirror_root)
            
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    raw = f.read()
                
                record = json.loads(raw)
                record['_relative_path'] = rel_path
                record['_file_path'] = fpath
                
                # Validate required fields
                missing = []
                for field in ['stable_id', 'kind', 'source', 'content']:
                    if field not in record:
                        missing.append(field)
                
                if missing:
                    record['_read_error'] = f"missing_fields: {', '.join(missing)}"
                else:
                    record['_read_error'] = None
                
                records.append(record)
                
            except json.JSONDecodeError as e:
                records.append({
                    '_relative_path': rel_path,
                    '_file_path': fpath,
                    '_read_error': f'json_parse_error: {str(e)}',
                    'stable_id': '',
                    'kind': '',
                })
            except Exception as e:
                records.append({
                    '_relative_path': rel_path,
                    '_file_path': fpath,
                    '_read_error': f'read_error: {str(e)}',
                    'stable_id': '',
                    'kind': '',
                })
    
    return records


# ── Record Processing ──────────────────────────────────────────────────

def process_records(records: List[Dict]) -> Tuple[List[Dict], Dict]:
    """
    Process raw records into output-ready entries.
    
    Returns:
      - entries: list of processed entry dicts
      - stats: dict with processing statistics
    """
    entries = []
    stats = {
        'total_files': len(records),
        'valid_records': 0,
        'errors': 0,
        'duplicate_ids': 0,
        'skipped_empty': 0,
    }
    
    seen_ids: Dict[str, int] = {}  # stable_id -> first occurrence index
    
    for i, rec in enumerate(records):
        if rec.get('_read_error'):
            stats['errors'] += 1
            # Still include errored records for transparency
            entries.append({
                'stable_id': rec.get('stable_id') or 'UNKNOWN',
                'kind': rec.get('kind') or 'unknown',
                'time': '',
                'role': '',
                'sender_display': '(error)',
                'preview': f"[{rec['_read_error']}]",
                'legacy_paths': [],
                'relative_mirror_file': rec.get('_relative_path', ''),
                '_error': rec['_read_error'],
            })
            continue
        
        kind = rec.get('kind', '')
        source = rec.get('source', {})
        content = rec.get('content', '')
        stable_id = rec.get('stable_id', '')

        # A malformed record must not abort indexing of the remaining mirror.
        if not isinstance(source, dict):
            error = 'invalid source: expected object'
            stats['errors'] += 1
            entries.append({
                'stable_id': stable_id or 'UNKNOWN',
                'kind': MIRROR_KIND_MAP.get(kind, kind or 'unknown'),
                'time': '',
                'role': '',
                'sender_display': '(error)',
                'preview': f'[{error}]',
                'legacy_paths': [],
                'relative_mirror_file': rec.get('_relative_path', ''),
                '_error': error,
            })
            continue
        
        # Map kind code to full name
        kind_display = MIRROR_KIND_MAP.get(kind, kind)
        
        # Time (mirror stores UTC; browse shows UTC+8 wall clock)
        timestamp_raw = source.get('timestamp', rec.get('written_at', ''))
        timestamp = format_display_time(timestamp_raw)
        
        # Role
        role = source.get('role', '')
        
        # Sender display (sanitized)
        sender_id = source.get('sender_id', '')
        sender_display = sanitize_sender(sender_id)
        
        # Preview (truncated)
        preview = truncate_preview(content)
        
        # Legacy paths (sanitized: strip path traversal)
        legacy_paths = rec.get('legacy_paths', [])
        if not isinstance(legacy_paths, list):
            legacy_paths = [str(legacy_paths)]
        # Normalize: strip leading traversal but keep for display (don't resolve)
        sanitized_legacy = []
        for lp in legacy_paths:
            lp_str = str(lp)
            # Remove obvious traversal patterns
            while lp_str.startswith('../') or lp_str.startswith('..\\'):
                lp_str = lp_str[3:] if lp_str.startswith('../') else lp_str[3:]
            sanitized_legacy.append(lp_str)
        
        entry = {
            'stable_id': stable_id,
            'kind': kind_display,
            'time': timestamp,
            'role': role,
            'sender_display': sender_display,
            'preview': preview,
            'legacy_paths': sanitized_legacy,
            'relative_mirror_file': rec.get('_relative_path', ''),
            '_error': None,
        }
        
        # Duplicate ID detection
        if stable_id in seen_ids:
            stats['duplicate_ids'] += 1
            entry['_duplicate_of_index'] = seen_ids[stable_id]
        else:
            seen_ids[stable_id] = len(entries)
        
        entries.append(entry)
        stats['valid_records'] += 1
    
    return entries, stats


# ── Output Generators ──────────────────────────────────────────────────

def generate_index_json(entries: List[Dict], stats: Dict, mirror_root: str) -> str:
    """Generate the index.json content."""
    output = {
        'schema': 'G4W.b0_browser_index.v1',
        # Display clock for local browse; generation folder names stay UTC.
        'generated_at': format_display_time(datetime.datetime.now(datetime.timezone.utc)),
        'display_timezone': DISPLAY_TZ_LABEL,
        'mirror_root': mirror_root,
        'statistics': stats,
        'entries': entries,
    }
    return json.dumps(output, ensure_ascii=False, indent=2)


def _sanitize_csv_field(value: str) -> str:
    """Prepare a value for CSV: collapse newlines, apply formula protection, quote."""
    if value is None:
        return ""
    s = str(value)
    # Collapse newlines for single-line CSV compatibility
    s = s.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    # Formula injection protection
    if s and s[0] in CSV_FORMULA_TRIGGER_CHARS:
        s = "'" + s
    # CSV quoting
    if ',' in s or '"' in s:
        s = '"' + s.replace('"', '""') + '"'
    return s


def generate_messages_csv(entries: List[Dict]) -> str:
    """Generate CSV with formula injection protection."""
    lines = []
    
    # Header
    headers = ['stable_id', 'kind', 'time', 'role', 'sender_display', 'preview', 
               'legacy_paths', 'relative_mirror_file', 'error']
    lines.append(','.join(csv_escape_field(h) for h in headers))
    
    # Data rows
    for entry in entries:
        row = [
            entry.get('stable_id') or '',
            entry.get('kind') or '',
            entry.get('time') or '',
            entry.get('role') or '',
            entry.get('sender_display') or '',
            entry.get('preview') or '',
            '|'.join(entry.get('legacy_paths', [])),
            entry.get('relative_mirror_file') or '',
            entry.get('_error') or '',
        ]
        lines.append(','.join(_sanitize_csv_field(str(v)) for v in row))
    
    return '\n'.join(lines) + '\n'


def _script_safe_json(value) -> str:
    """Serialize JSON for an HTML script data block without parser breakouts."""
    return (json.dumps(value, ensure_ascii=False)
            .replace('&', r'\u0026')
            .replace('<', r'\u003c')
            .replace('>', r'\u003e')
            .replace('\u2028', r'\u2028')
            .replace('\u2029', r'\u2029'))


def generate_index_html(entries: List[Dict], stats: Dict, mirror_root: str) -> str:
    """
    Generate the browsable index.html.
    
    SECURITY: All dynamic content is embedded via JSON serialization and
    populated using textContent (never innerHTML). No inline event handlers.
    CSP meta tag forbids all external resources and scripts.
    """
    
    # Escape HTML parser delimiters as JSON unicode escapes before script embedding.
    entries_json = _script_safe_json(entries)
    stats_json = _script_safe_json(stats)
    mirror_root_json = _script_safe_json(mirror_root)
    generated_at_display = format_display_time(datetime.datetime.now(datetime.timezone.utc))
    generated_at_json = _script_safe_json(generated_at_display)
    preview_max = PREVIEW_MAX_CHARS
    
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="{CSP_POLICY}">
<title>G4W B0 - Mirror Browser Index</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC", sans-serif; background: #f5f5f5; color: #333; }}
  .header {{ background: #1a1a2e; color: #e0e0e0; padding: 1rem 1.5rem; }}
  .header h1 {{ font-size: 1.3rem; font-weight: 600; }}
  .header .sub {{ font-size: 0.8rem; opacity: 0.7; margin-top: 0.25rem; }}
  .stats {{ display: flex; gap: 1rem; padding: 0.75rem 1.5rem; background: #16213e; color: #a0a0b0; font-size: 0.8rem; flex-wrap: wrap; }}
  .stats span {{ background: #0f3460; padding: 0.2rem 0.6rem; border-radius: 3px; }}
  .search-bar {{ padding: 0.75rem 1.5rem; background: #fff; border-bottom: 1px solid #e0e0e0; position: sticky; top: 0; z-index: 10; }}
  .search-bar input {{ width: 100%; padding: 0.5rem 0.75rem; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9rem; outline: none; }}
  .search-bar input:focus {{ border-color: #1a1a2e; box-shadow: 0 0 0 2px rgba(26,26,46,0.15); }}
  .filter-row {{ display: flex; gap: 0.5rem; margin-top: 0.5rem; flex-wrap: wrap; }}
  .filter-row select, .filter-row button {{ padding: 0.35rem 0.6rem; border: 1px solid #ccc; border-radius: 4px; font-size: 0.8rem; background: #fff; cursor: pointer; }}
  .filter-row button {{ background: #1a1a2e; color: #fff; border-color: #1a1a2e; }}
  .filter-row button:hover {{ background: #16213e; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; }}
  thead {{ position: sticky; top: 106px; z-index: 5; }}
  th {{ background: #1a1a2e; color: #e0e0e0; padding: 0.5rem 0.75rem; font-size: 0.8rem; text-align: left; font-weight: 500; cursor: pointer; user-select: none; white-space: nowrap; }}
  th:hover {{ background: #16213e; }}
  td {{ padding: 0.5rem 0.75rem; font-size: 0.82rem; border-bottom: 1px solid #eee; vertical-align: top; }}
  tr:hover td {{ background: #f8f8ff; }}
  .col-id {{ font-family: "SF Mono", "Cascadia Code", "Consolas", monospace; font-size: 0.75rem; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .col-kind {{ text-transform: uppercase; font-size: 0.7rem; font-weight: 600; }}
  .col-kind .badge {{ display: inline-block; padding: 0.15rem 0.4rem; border-radius: 3px; }}
  .badge-conversation {{ background: #e3f2fd; color: #1565c0; }}
  .badge-round {{ background: #fce4ec; color: #c62828; }}
  .col-preview {{ max-width: 350px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .col-paths {{ font-size: 0.7rem; color: #666; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .col-error {{ color: #c62828; font-size: 0.75rem; }}
  .row-error td {{ background: #fff5f5; }}
  .row-dup td {{ background: #fffde7; }}
  .empty-state {{ text-align: center; padding: 3rem 1rem; color: #999; }}
  .footer {{ padding: 1rem 1.5rem; font-size: 0.7rem; color: #999; text-align: center; border-top: 1px solid #eee; }}
  .tooltip {{ position: relative; cursor: help; border-bottom: 1px dotted #666; }}
  @media (max-width: 768px) {{
    th, td {{ font-size: 0.7rem; padding: 0.35rem 0.4rem; }}
    .col-preview, .col-paths {{ max-width: 120px; }}
  }}
</style>
</head>
<body>
<div class="header">
  <h1>G4W B0 — Mirror Browser Index</h1>
  <div class="sub">Read-only view of short-path mirror records</div>
</div>
<div class="stats" id="stats-bar">
  <span>Total: <strong id="stat-total">0</strong></span>
  <span>Valid: <strong id="stat-valid">0</strong></span>
  <span>Errors: <strong id="stat-errors">0</strong></span>
  <span>Duplicates: <strong id="stat-dupes">0</strong></span>
</div>
<div class="search-bar">
  <input type="text" id="search-input" placeholder="Search by ID, sender, content, or path..." autocomplete="off">
  <div class="filter-row">
    <select id="filter-kind">
      <option value="">All kinds</option>
      <option value="conversation">Conversation</option>
      <option value="round">Round</option>
    </select>
    <select id="filter-role">
      <option value="">All roles</option>
      <option value="user">User</option>
      <option value="assistant">Assistant</option>
      <option value="system">System</option>
    </select>
    <button id="btn-reset">Reset Filters</button>
    <button id="btn-export-csv">Export CSV</button>
  </div>
</div>
<table id="data-table">
  <thead>
    <tr>
      <th data-sort="stable_id">Stable ID ▾</th>
      <th data-sort="kind">Kind ▾</th>
      <th data-sort="time">Time (UTC+8) ▾</th>
      <th data-sort="role">Role ▾</th>
      <th data-sort="sender_display">Sender ▾</th>
      <th data-sort="preview">Preview ▾</th>
      <th data-sort="legacy_paths">Legacy Paths ▾</th>
      <th data-sort="relative_mirror_file">Mirror File ▾</th>
    </tr>
  </thead>
  <tbody id="table-body">
  </tbody>
</table>
<div class="empty-state" id="empty-state" style="display:none">No records match the current filters.</div>
<div class="footer">
  Generated <span id="gen-time"></span> ({DISPLAY_TZ_LABEL}) &mdash; Mirror: <span id="mirror-root-display"></span>
  &mdash; <strong>No external requests</strong> &middot; CSP enforced &middot; Read-only
  &middot; Entry times shown in {DISPLAY_TZ_LABEL}
</div>

<!-- All data embedded via JSON serialization - no eval, no innerHTML from data -->
<script type="application/json" id="entries-data">
{entries_json}
</script>
<script type="application/json" id="stats-data">
{stats_json}
</script>
<script type="application/json" id="mirror-root-data">
{mirror_root_json}
</script>
<script type="application/json" id="generated-at-data">
{generated_at_json}
</script>

<script>
(function() {{
  "use strict";

  // ── Read embedded data safely via JSON ──
  var ENTRIES = JSON.parse(document.getElementById('entries-data').textContent);
  var STATS = JSON.parse(document.getElementById('stats-data').textContent);
  var MIRROR_ROOT = JSON.parse(document.getElementById('mirror-root-data').textContent);
  var PREVIEW_MAX = {preview_max};

  // ── DOM refs ──
  var tbody = document.getElementById('table-body');
  var emptyState = document.getElementById('empty-state');
  var searchInput = document.getElementById('search-input');
  var filterKind = document.getElementById('filter-kind');
  var filterRole = document.getElementById('filter-role');

  // ── State ──
  var currentSort = {{ field: 'time', asc: false }};
  var filteredEntries = ENTRIES.slice();

  // ── Stats display ──
  document.getElementById('stat-total').textContent = STATS.total_files;
  document.getElementById('stat-valid').textContent = STATS.valid_records;
  document.getElementById('stat-errors').textContent = STATS.errors;
  document.getElementById('stat-dupes').textContent = STATS.duplicate_ids;
  // Prefer build-time UTC+8 stamp (not browser local/UTC ISO).
  var genAtEl = document.getElementById('generated-at-data');
  var genAt = '';
  try {{ genAt = genAtEl ? JSON.parse(genAtEl.textContent) : ''; }} catch (e) {{ genAt = ''; }}
  document.getElementById('gen-time').textContent = genAt || '';
  document.getElementById('mirror-root-display').textContent = MIRROR_ROOT;

  // ── Table builder (all via textContent, NO innerHTML from data) ──
  function buildRow(entry) {{
    var tr = document.createElement('tr');
    if (entry._error) tr.className = 'row-error';
    if (entry._duplicate_of_index !== undefined) tr.className = 'row-dup';

    // Stable ID cell
    var tdId = document.createElement('td');
    tdId.className = 'col-id';
    tdId.textContent = entry.stable_id || '';
    if (entry._duplicate_of_index !== undefined) {{
      tdId.title = 'DUPLICATE of entry #' + (entry._duplicate_of_index + 1);
    }}
    tr.appendChild(tdId);

    // Kind cell
    var tdKind = document.createElement('td');
    tdKind.className = 'col-kind';
    var badge = document.createElement('span');
    badge.className = 'badge badge-' + (entry.kind || 'unknown');
    badge.textContent = (entry.kind || '').toUpperCase();
    tdKind.appendChild(badge);
    tr.appendChild(tdKind);

    // Time cell
    var tdTime = document.createElement('td');
    tdTime.textContent = entry.time || '';
    tr.appendChild(tdTime);

    // Role cell
    var tdRole = document.createElement('td');
    tdRole.textContent = entry.role || '';
    tr.appendChild(tdRole);

    // Sender cell
    var tdSender = document.createElement('td');
    tdSender.textContent = entry.sender_display || '';
    tr.appendChild(tdSender);

    // Preview cell
    var tdPreview = document.createElement('td');
    tdPreview.className = 'col-preview';
    tdPreview.textContent = entry.preview || '';
    tdPreview.title = entry.preview || '';
    tr.appendChild(tdPreview);

    // Legacy paths cell
    var tdPaths = document.createElement('td');
    tdPaths.className = 'col-paths';
    tdPaths.textContent = (entry.legacy_paths || []).join(' | ');
    tdPaths.title = tdPaths.textContent;
    tr.appendChild(tdPaths);

    // Mirror file cell
    var tdFile = document.createElement('td');
    tdFile.className = 'col-paths';
    tdFile.textContent = entry.relative_mirror_file || '';
    tdFile.title = tdFile.textContent;
    tr.appendChild(tdFile);

    return tr;
  }}

  function renderTable() {{
    tbody.textContent = '';  // safe clear
    if (filteredEntries.length === 0) {{
      emptyState.style.display = 'block';
      return;
    }}
    emptyState.style.display = 'none';
    var fragment = document.createDocumentFragment();
    for (var i = 0; i < filteredEntries.length; i++) {{
      fragment.appendChild(buildRow(filteredEntries[i]));
    }}
    tbody.appendChild(fragment);
  }}

  // ── Filtering ──
  function applyFilters() {{
    var query = searchInput.value.toLowerCase().trim();
    var kindVal = filterKind.value;
    var roleVal = filterRole.value;

    filteredEntries = ENTRIES.filter(function(e) {{
      if (kindVal && e.kind !== kindVal) return false;
      if (roleVal && e.role !== roleVal) return false;
      if (query) {{
        var haystack = (e.stable_id + ' ' + e.sender_display + ' ' + e.preview + ' ' +
                       (e.legacy_paths || []).join(' ') + ' ' + e.relative_mirror_file).toLowerCase();
        if (haystack.indexOf(query) === -1) return false;
      }}
      return true;
    }});

    applySort();
    renderTable();
  }}

  // ── Sorting ──
  function applySort() {{
    var field = currentSort.field;
    var asc = currentSort.asc;
    filteredEntries.sort(function(a, b) {{
      var va = (a[field] || '').toString().toLowerCase();
      var vb = (b[field] || '').toString().toLowerCase();
      if (va < vb) return asc ? -1 : 1;
      if (va > vb) return asc ? 1 : -1;
      return 0;
    }});
  }}

  // ── Event bindings (no inline handlers) ──
  searchInput.addEventListener('input', function() {{ applyFilters(); }});
  filterKind.addEventListener('change', function() {{ applyFilters(); }});
  filterRole.addEventListener('change', function() {{ applyFilters(); }});

  document.getElementById('btn-reset').addEventListener('click', function() {{
    searchInput.value = '';
    filterKind.value = '';
    filterRole.value = '';
    currentSort = {{ field: 'time', asc: false }};
    applyFilters();
  }});

  document.getElementById('btn-export-csv').addEventListener('click', function() {{
    // Build CSV client-side (formula-safe)
    var headers = ['stable_id','kind','time','role','sender_display','preview','legacy_paths','relative_mirror_file','error'];
    var lines = [headers.join(',')];
    for (var i = 0; i < filteredEntries.length; i++) {{
      var e = filteredEntries[i];
      var row = [
        e.stable_id || '', e.kind || '', e.time || '', e.role || '',
        e.sender_display || '', e.preview || '',
        (e.legacy_paths || []).join('|'), e.relative_mirror_file || '', e._error || ''
      ];
      // CSV formula protection on each field
      var safeRow = row.map(function(v) {{
        var s = String(v);
        if (s && ('=+-@'.indexOf(s[0]) !== -1)) s = "'" + s;
        if (s.indexOf(',') !== -1 || s.indexOf('"') !== -1 || s.indexOf('\\n') !== -1) {{
          s = '"' + s.replace(/"/g, '""') + '"';
        }}
        return s;
      }});
      lines.push(safeRow.join(','));
    }}
    var blob = new Blob([lines.join('\\n') + '\\n'], {{ type: 'text/csv;charset=utf-8' }});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'messages_export.csv';
    a.click();
    URL.revokeObjectURL(url);
  }});

  // Column header sort
  var headers = document.querySelectorAll('th[data-sort]');
  headers.forEach(function(th) {{
    th.addEventListener('click', function() {{
      var field = this.getAttribute('data-sort');
      if (currentSort.field === field) {{
        currentSort.asc = !currentSort.asc;
      }} else {{
        currentSort.field = field;
        currentSort.asc = true;
      }}
      applySort();
      renderTable();
      // Update header indicators
      headers.forEach(function(h) {{ h.textContent = h.getAttribute('data-sort').replace(/_/g, ' '); }});
      this.textContent = this.getAttribute('data-sort').replace(/_/g, ' ') + (currentSort.asc ? ' ▴' : ' ▾');
    }});
  }});

  // ── Initial render ──
  applyFilters();

}})();
</script>
</body>
</html>'''

    # CSP hashes cover the exact text node of the executable script, including
    # the newlines immediately inside the <script> element.
    script_open = '<script>\n'
    script_close = '\n</script>'
    script_start = html.rfind(script_open)
    if script_start < 0:
        raise RuntimeError('application script marker missing')
    script_start += len('<script>')
    script_end = html.find('</script>', script_start)
    script_text = html[script_start:script_end]
    digest = base64.b64encode(hashlib.sha256(script_text.encode('utf-8')).digest()).decode('ascii')
    html = html.replace('__B0_SCRIPT_HASH__', 'sha256-' + digest, 1)
    return html


# ── Consistent generation publication ─────────────────────────────────

def atomic_write(filepath: str, content: str):
    """
    Write exact UTF-8 bytes to a temp file, then atomically replace it.

    Binary mode is intentional: text mode rewrites LF to CRLF on Windows,
    invalidating byte-level manifests and inline-script CSP hashes.
    """
    dirpath = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(dirpath, exist_ok=True)
    payload = content.encode('utf-8')
    
    # Create temp file in same directory (same filesystem for atomic replace)
    tmp_name = os.path.join(dirpath, f".tmp_{uuid.uuid4().hex}.b0")
    try:
        with open(tmp_name, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        
        # Atomic replace on same filesystem
        os.replace(tmp_name, filepath)
    except Exception:
        # Cleanup temp file on failure
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise


def publish_generation(output_dir: str, artifacts: Dict[str, str], fail_after: Optional[str] = None) -> str:
    """Publish an immutable generation, CURRENT, then the stable file entry.

    Collection readers read CURRENT once and then stay below that generation.
    Local users open <output>/index.html directly; it is a self-contained
    snapshot and is atomically refreshed only after CURRENT commits.  A failure
    before CURRENT preserves both old views.  A failure at ``root_index`` may
    leave CURRENT at the new complete generation while the root entry remains
    the complete old snapshot; the call fails explicitly and a retry converges.
    A successful return guarantees CURRENT and the root entry contain the same
    generation's HTML.  Already-open file:// documents naturally keep their
    prior in-memory snapshot across later publications.
    """
    output = Path(output_dir)
    generations = output / 'generations'
    output.mkdir(parents=True, exist_ok=True)
    generations.mkdir(parents=True, exist_ok=True)
    generation = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + uuid.uuid4().hex
    staging = generations / ('.tmp-' + generation)
    final = generations / generation
    staging.mkdir()
    try:
        for name in ('index.json', 'messages.csv', 'index.html'):
            if name not in artifacts:
                raise ValueError('missing artifact: ' + name)
            atomic_write(str(staging / name), artifacts[name])
            if fail_after == name:
                raise RuntimeError('injected publication failure after ' + name)
        # Build the manifest from bytes read back from disk, not from the
        # in-memory strings.  This detects write/encoding corruption before
        # the generation becomes visible through CURRENT.
        artifact_hashes = {}
        artifact_sizes = {}
        for name in ('index.json', 'messages.csv', 'index.html'):
            payload = (staging / name).read_bytes()
            expected = artifacts[name].encode('utf-8')
            if payload != expected:
                raise IOError('artifact byte verification failed: ' + name)
            artifact_hashes[name] = hashlib.sha256(payload).hexdigest()
            artifact_sizes[name] = len(payload)
        manifest = {
            'generation': generation,
            'artifacts': artifact_hashes,
            'sizes': artifact_sizes,
        }
        atomic_write(str(staging / 'manifest.json'), json.dumps(manifest, ensure_ascii=False, indent=2))
        if fail_after == 'manifest':
            raise RuntimeError('injected publication failure after manifest')
        os.replace(str(staging), str(final))
        # Collection commit point. CURRENT is the consistency boundary for
        # readers that need index.json/CSV/HTML as one immutable set.
        atomic_write(str(output / 'CURRENT'), generation + '\n')
        # Browser entry commit point. The HTML is self-contained, so replacing
        # this one file can never expose a partial page or mixed page/data.
        if fail_after == 'root_index':
            raise RuntimeError('injected publication failure before root index')
        atomic_write(str(output / 'index.html'), artifacts['index.html'])
        return generation
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def resolve_current_generation(output_dir: str) -> Path:
    """Resolve and validate the currently committed immutable generation."""
    output = Path(output_dir).resolve()
    generation = (output / 'CURRENT').read_text(encoding='ascii').strip()
    if not generation or any(c not in '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.-' for c in generation):
        raise ValueError('invalid CURRENT generation')
    path = (output / 'generations' / generation).resolve()
    if path.parent != (output / 'generations').resolve() or not path.is_dir():
        raise ValueError('CURRENT does not name a valid generation')
    return path


# ── Main ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <mirror_root> [output_dir]", file=sys.stderr)
        print("  mirror_root: path to short-path-mirror root (containing m/)", file=sys.stderr)
        print("  output_dir:  where to write index artifacts (default: mirror_root/browse/)", file=sys.stderr)
        sys.exit(1)
    
    mirror_root = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(mirror_root, 'browse')
    
    print(f"B0 Index Generator")
    print(f"  Mirror root : {mirror_root}")
    print(f"  Output dir  : {output_dir}")
    print()
    
    # Verify mirror root
    mirror_root = verify_mirror_root(mirror_root)
    print(f"[OK] Mirror root verified: {mirror_root}")
    
    # Read records
    print("Reading mirror records...")
    records = read_mirror_records(mirror_root)
    print(f"  Found {len(records)} JSON files")
    
    # Process
    print("Processing records...")
    entries, stats = process_records(records)
    print(f"  Valid: {stats['valid_records']}, Errors: {stats['errors']}, Duplicates: {stats['duplicate_ids']}")
    
    # Generate all outputs before publishing any of them.
    print("Generating outputs...")
    artifacts = {
        'index.json': generate_index_json(entries, stats, mirror_root),
        'messages.csv': generate_messages_csv(entries),
        'index.html': generate_index_html(entries, stats, mirror_root),
    }
    generation = publish_generation(output_dir, artifacts)
    generation_dir = Path(output_dir) / 'generations' / generation
    for name, content in artifacts.items():
        print(f"  [OK] {name} ({len(content)} bytes)")

    print()
    print("B0 index generation complete.")
    print(f"Output directory: {output_dir}")
    print(f"Current generation: {generation_dir}")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
