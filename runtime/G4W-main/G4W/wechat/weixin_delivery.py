import re


MAX_WEIXIN_CHUNK = 3800
DEFAULT_MIN_WEIXIN_CHUNK = 10
WEIXIN_LIVE_DELIVERY_MESSAGES = 8
WEIXIN_MAX_DELIVERY_MESSAGES = 10
DEFERRED_TAIL_NOTICE = "后面还有一段内容比较长，先暂存起来；等你下一条消息刷新微信 context_token 后，我会继续补发。"
DEFERRED_REPLY_NOTICE = "由于微信 context_token 的限制，上轮对话里有一部分内容当时没能送达；这次用户再次发来消息、context_token 刷新后，先把遗留内容补上。如果这种情况反复出现，可发送 /chunk <数字>（例如 /chunk 50）调大最小合并字符数，减少消息分片。"
DEFERRED_PLAIN_REPLY_HEADER = "===== 上轮对话遗留内容 ====="
DEFERRED_SYSTEM_REPLY_HEADER = "===== 期间模型主动联系 ====="
DEFERRED_PROACTIVE_REPORT_HEADER = "===== 期间模型主动汇报 ====="
CURRENT_REPLY_HEADER = "===== 本轮模型回复 ====="


def trim_outer_blank_lines(text: str) -> str:
    return re.sub(r"\n+\s*$", "", re.sub(r"^\s*\n+", "", str(text or "")))


def split_utf8(text: str, max_chars: int = MAX_WEIXIN_CHUNK) -> list[str]:
    value = str(text or "")
    return [value[index:index + max_chars] for index in range(0, len(value), max_chars)] or [""]


def compact_plain_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n")
    return trim_outer_blank_lines(re.sub(r"\n\s*\n+", "\n", normalized))


def strip_sentence_tail_chinese_full_stops(text: str) -> str:
    return "\n".join(re.sub(r"。+(?=(?:\s*[\"'）)\]」』】])*\s*$)", "", line) for line in str(text or "").split("\n"))


def indent_block(text: str) -> str:
    return "\n".join(f"    {line}" for line in str(text or "").strip("\n").split("\n"))


def markdown_to_plain_text(text: str) -> str:
    result = str(text or "").replace("\r\n", "\n")

    def code_block(match):
        language = str(match.group(1) or "").strip()
        label = f"{language}:" if language else "Code:"
        return f"\n{label}\n{indent_block(match.group(2))}\n"

    result = re.sub(r"```([^\n]*)\n?([\s\S]*?)```", code_block, result)
    result = re.sub(r"```([^\n]*)\n?([\s\S]*)$", code_block, result)
    result = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", result)
    result = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", result)
    result = re.sub(r"`([^`]+)`", r"\1", result)
    result = re.sub(r"^#{1,6}\s*(.+)$", r"\1", result, flags=re.M)
    result = re.sub(r"\*\*([^*]+)\*\*", r"\1", result)
    result = re.sub(r"\*([^*]+)\*", r"\1", result)
    result = re.sub(r"^>\s?", "> ", result, flags=re.M)
    result = re.sub(r"^\|[\s:|-]+\|$", "", result, flags=re.M)
    result = re.sub(
        r"^\|(.+)\|$",
        lambda match: "  ".join(cell.strip() for cell in match.group(1).split("|")),
        result,
        flags=re.M,
    )
    return trim_outer_blank_lines(re.sub(r"\n{3,}", "\n\n", result))


def has_structural_markdown(text: str) -> bool:
    normalized = str(text or "").replace("\r\n", "\n")
    if not normalized.strip():
        return False
    patterns = (
        r"(^|\n)\s*(?:```|~~~)",
        r"(^|\n)\s{0,3}#{1,6}\s+\S",
        r"(^|\n)\s{0,3}(?:[-*+]\s+|\d+[.)]\s+)\S",
        r"(^|\n)\s{0,3}>\s+\S",
        r"(^|\n)\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*(?=\n|$)",
        r"(^|[^*])\*\*(?=\S).*?\S\*\*(?!\*)",
        r"(^|[^_])__(?=\S).*?\S__(?!_)",
    )
    if any(re.search(pattern, normalized, flags=re.M | re.S) for pattern in patterns):
        return True
    lines = normalized.split("\n")
    for index in range(len(lines) - 1):
        header = lines[index].strip()
        separator = lines[index + 1].strip()
        if header.count("|") >= 2 and re.fullmatch(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?", separator):
            return True
    return False


def chunk_reply_text(text: str, limit: int = 3500) -> list[str]:
    normalized = trim_outer_blank_lines(str(text or "").replace("\r\n", "\n"))
    if not normalized.strip():
        return []
    chunks = []
    remaining = normalized
    while len(remaining) > limit:
        candidate = remaining[:limit]
        choices = [candidate.rfind("\n\n"), candidate.rfind("\n"), candidate.rfind("。"), candidate.rfind(". "), candidate.rfind(" ")]
        split_index = max(choices)
        cut = split_index + (0 if split_index >= 0 and candidate[split_index] == "\n" else 1) if split_index > limit * 0.4 else limit
        chunk = trim_outer_blank_lines(remaining[:cut])
        if chunk.strip():
            chunks.append(chunk)
        remaining = trim_outer_blank_lines(remaining[cut:])
    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


def collect_streaming_boundaries(text: str) -> list[int]:
    boundaries = set()
    for match in re.finditer(r"\n\s*\n+", text):
        boundaries.add(match.end())
    for match in re.finditer(r"\n(?:(?:[-*])\s+|(?:\d+\.)\s+)", text):
        boundaries.add(match.start() + 1)
    closing = set('\"\'）)]」』】')
    for index, char in enumerate(text):
        if char not in "。！？!?":
            continue
        end = index + 1
        while end < len(text) and text[end] in closing:
            end += 1
        while end < len(text) and text[end] in "\t \n":
            end += 1
        boundaries.add(end)
    return sorted(boundaries)


def merge_short_chunks(chunks: list[str], max_length: int, min_length: int) -> list[str]:
    if not chunks:
        return []
    merged = []
    buffer = chunks[0]
    for chunk in chunks[1:]:
        joined = f"{buffer}\n{chunk}"
        if len(buffer) < min_length and len(chunk) < min_length and len(joined) <= max_length:
            buffer = joined
        else:
            merged.append(buffer)
            buffer = chunk
    merged.append(buffer)
    return merged


def chunk_reply_text_for_weixin(text: str, min_chunk: int = DEFAULT_MIN_WEIXIN_CHUNK) -> list[str]:
    normalized = trim_outer_blank_lines(str(text or "").replace("\r\n", "\n"))
    if not normalized.strip():
        return []
    boundaries = collect_streaming_boundaries(normalized)
    if not boundaries:
        return chunk_reply_text(normalized, MAX_WEIXIN_CHUNK)
    units = []
    start = 0
    for boundary in boundaries:
        if boundary <= start:
            continue
        unit = trim_outer_blank_lines(normalized[start:boundary])
        if unit:
            units.append(unit)
        start = boundary
    tail = trim_outer_blank_lines(normalized[start:])
    if tail:
        units.append(tail)
    chunks = []
    for unit in units:
        chunks.extend([unit] if len(unit) <= MAX_WEIXIN_CHUNK else chunk_reply_text(unit, MAX_WEIXIN_CHUNK))
    return merge_short_chunks([chunk for chunk in chunks if chunk], MAX_WEIXIN_CHUNK, max(1, min(int(min_chunk), MAX_WEIXIN_CHUNK)))


def prepare_reply_chunks(text: str, min_chunk: int = DEFAULT_MIN_WEIXIN_CHUNK) -> tuple[list[str], bool]:
    normalized = trim_outer_blank_lines(str(text or "").replace("\r\n", "\n"))
    preserve_markdown = has_structural_markdown(normalized)
    if preserve_markdown:
        return [chunk for chunk in split_utf8(normalized) if chunk], True
    plain = markdown_to_plain_text(normalized)
    return chunk_reply_text_for_weixin(plain, min_chunk), False


def apply_delivery_budget(
    chunks: list[str],
    max_messages: int = WEIXIN_MAX_DELIVERY_MESSAGES,
    live_messages: int = WEIXIN_LIVE_DELIVERY_MESSAGES,
    preserve_markdown: bool = False,
    sent_count: int = 0,
) -> tuple[list[str], str]:
    normalizer = (lambda value: str(value or "").strip()) if preserve_markdown else compact_plain_text
    normalized = [normalizer(chunk) for chunk in chunks]
    normalized = [chunk for chunk in normalized if chunk]
    sent_count = max(0, int(sent_count or 0))
    live_remaining = max(0, live_messages - sent_count)
    total_remaining = max(0, max_messages - sent_count)
    if not total_remaining:
        return [], ("\n\n" if preserve_markdown else "\n").join(normalized)
    if len(normalized) <= live_remaining:
        return normalized, ""

    # Match the original two-stage WeChat budget: bubbles 1-8 stay separate;
    # everything remaining is aggregated into bubble 9, and only spills into
    # bubble 10 when the 3800-character API limit requires it.
    head = normalized[:live_remaining]
    separator = "\n\n" if preserve_markdown else "\n"
    tail = separator.join(normalized[live_remaining:])
    tail_chunks = [chunk for chunk in split_utf8(tail, MAX_WEIXIN_CHUNK) if chunk]
    remaining_slots = max(0, total_remaining - len(head))
    sent_tail = tail_chunks[:remaining_slots]
    deferred = separator.join(tail_chunks[remaining_slots:])
    return head + sent_tail, deferred


def normalize_delivery_chunks(chunks: list[str], preserve_markdown: bool = False) -> list[str]:
    normalizer = (lambda value: str(value or "").strip()) if preserve_markdown else compact_plain_text
    return [value for value in (normalizer(chunk) for chunk in chunks or []) if value]


def join_delivery_chunks(chunks: list[str], preserve_markdown: bool = False) -> str:
    return ("\n\n" if preserve_markdown else "\n").join(normalize_delivery_chunks(chunks, preserve_markdown))


def take_live_delivery(
    chunks: list[str],
    sent_count: int = 0,
    live_messages: int = WEIXIN_LIVE_DELIVERY_MESSAGES,
    preserve_markdown: bool = False,
) -> tuple[list[str], list[str]]:
    normalized = normalize_delivery_chunks(chunks, preserve_markdown)
    remaining = max(0, int(live_messages) - max(0, int(sent_count or 0)))
    return normalized[:remaining], normalized[remaining:]


def pack_final_burst(
    chunks: list[str],
    sent_count: int = 0,
    max_messages: int = WEIXIN_MAX_DELIVERY_MESSAGES,
    max_chars: int = MAX_WEIXIN_CHUNK,
    preserve_markdown: bool = False,
) -> tuple[list[str], str]:
    normalized = normalize_delivery_chunks(chunks, preserve_markdown)
    remaining_slots = max(0, int(max_messages) - max(0, int(sent_count or 0)))
    separator = "\n\n" if preserve_markdown else "\n"
    if not remaining_slots:
        return [], separator.join(normalized)
    # Finalization is not itself a reason to collapse the whole answer.
    # Preserve bubbles 1-8 exactly as the streaming path does, then aggregate
    # only the remaining tail into bubble 9 (and bubble 10 on size overflow).
    live_remaining = max(0, WEIXIN_LIVE_DELIVERY_MESSAGES - max(0, int(sent_count or 0)))
    head = normalized[:live_remaining]
    tail = normalized[live_remaining:]
    if not tail:
        return head[:remaining_slots], separator.join(head[remaining_slots:])

    packed = []
    current = ""
    deferred_start = -1
    tail_slots = max(0, remaining_slots - len(head))

    if not tail_slots:
        return head[:remaining_slots], separator.join(tail)

    def push(value: str) -> bool:
        normalized_value = trim_outer_blank_lines(value)
        if not normalized_value:
            return True
        if len(packed) >= tail_slots:
            return False
        packed.append(normalized_value[:max_chars])
        return True

    for index, chunk in enumerate(tail):
        if len(chunk) > max_chars:
            if current:
                if not push(current):
                    deferred_start = max(0, index - 1)
                    break
                current = ""
            for hard_chunk in split_utf8(chunk, max_chars):
                if not push(hard_chunk):
                    deferred_start = index
                    break
            if deferred_start >= 0:
                break
            continue
        joined = f"{current}{separator}{chunk}" if current else chunk
        if len(joined) <= max_chars:
            current = joined
            continue
        if current and not push(current):
            deferred_start = max(0, index - 1)
            break
        current = chunk
    if current:
        if not push(current):
            deferred_start = max(0, len(tail) - 1)
    if deferred_start < 0:
        return head + packed, ""
    return head + (packed or [DEFERRED_TAIL_NOTICE]), separator.join(tail[deferred_start:])


def format_deferred_reply_batch(items: list[dict]) -> str:
    grouped = {"plain_reply": [], "system_reply": [], "proactive_report": []}
    for item in items or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        kind = str(item.get("kind") or "plain_reply")
        if kind == "checkin":
            kind = "system_reply"
        grouped.setdefault(kind, []).append(text)
    parts = [DEFERRED_REPLY_NOTICE]
    if grouped.get("plain_reply"):
        parts.extend(["", DEFERRED_PLAIN_REPLY_HEADER, "\n\n".join(grouped["plain_reply"])])
    if grouped.get("system_reply"):
        parts.extend(["", DEFERRED_SYSTEM_REPLY_HEADER, "\n\n".join(grouped["system_reply"])])
    if grouped.get("proactive_report"):
        parts.extend(["", DEFERRED_PROACTIVE_REPORT_HEADER, "\n\n".join(grouped["proactive_report"])])
    return "\n".join(parts)


def build_effective_reply_text(prefix: str, reply: str) -> str:
    old = trim_outer_blank_lines(str(prefix or "").replace("\r\n", "\n"))
    current = trim_outer_blank_lines(str(reply or "").replace("\r\n", "\n"))
    if old and current:
        return f"{old}\n\n{CURRENT_REPLY_HEADER}\n{current}"
    return old or current
