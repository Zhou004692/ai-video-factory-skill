"""Traceable, deduplicated source material for script generation."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import uuid
from urllib.parse import urljoin, urlparse

import httpx


MAX_SOURCE_CHARS = 2_000_000
_SUBTITLE_EXTENSIONS = {".srt", ".vtt"}
_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", *_SUBTITLE_EXTENSIONS}
_SRT_TIMECODE = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+"
    r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}.*$"
)
_VTT_TIMECODE = re.compile(
    r"^\s*(?:\d{1,2}:)?\d{2}:\d{2}[.]\d{3}\s+-->\s+"
    r"(?:\d{1,2}:)?\d{2}:\d{2}[.]\d{3}.*$"
)


@dataclass(frozen=True)
class IngestResult:
    source_id: str
    created: bool


def normalize_content(content: str) -> str:
    value = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in value.split("\n")]
    normalized: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if normalized and not blank:
                normalized.append("")
            blank = True
            continue
        normalized.append(line)
        blank = False
    while normalized and not normalized[-1]:
        normalized.pop()
    value = "\n".join(normalized)
    if not value:
        raise ValueError("script source content is empty")
    if len(value) > MAX_SOURCE_CHARS:
        raise ValueError(
            f"script source exceeds {MAX_SOURCE_CHARS} normalized characters"
        )
    return value


def compact_text_for_prompt(content: str, *, max_chars: int = 60_000) -> str:
    """Bound prompt size while sampling the whole source, never a silent prefix."""
    text = str(content or "")
    limit = max(200, int(max_chars))
    if len(text) <= limit:
        return text
    marker = "\n\n[... content omitted between sampled sections ...]\n\n"
    available = max(3, limit - (len(marker) * 2))
    head_size = available // 3
    middle_size = available // 3
    tail_size = available - head_size - middle_size
    middle_start = max(0, (len(text) - middle_size) // 2)
    return (
        text[:head_size]
        + marker
        + text[middle_start:middle_start + middle_size]
        + marker
        + text[-tail_size:]
    )[:limit]


def subtitle_to_text(content: str) -> str:
    kept: list[str] = []
    for raw in str(content or "").splitlines():
        line = raw.strip()
        if (not line or line == "WEBVTT" or line.isdigit()
                or _SRT_TIMECODE.match(line) or _VTT_TIMECODE.match(line)):
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and (not kept or kept[-1] != line):
            kept.append(line)
    return normalize_content("\n".join(kept))


def _fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def ingest_text(db, content: str, *, title: str | None = None,
                source_uri: str | None = None,
                source_type: str = "text",
                metadata: dict | None = None,
                deduplication_key: str | None = None) -> IngestResult:
    normalized = normalize_content(content)
    fingerprint = _fingerprint(deduplication_key or normalized)
    existing = db.execute(
        "SELECT source_id FROM script_sources WHERE fingerprint=?",
        (fingerprint,),
    ).fetchone()
    if existing:
        return IngestResult(str(existing["source_id"]), False)
    source_id = f"SRC-{fingerprint[:16]}"
    db.execute(
        "INSERT INTO script_sources(source_id, fingerprint, source_type, "
        "title, source_uri, content, metadata_json) VALUES(?,?,?,?,?,?,?)",
        (
            source_id, fingerprint, source_type, str(title or "").strip() or None,
            str(source_uri or "").strip() or None, normalized,
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    db.commit()
    return IngestResult(source_id, True)


def instantiate_content_source(
        db, source_id: str, *, title: str | None = None) -> IngestResult:
    """Create an independent project root while reusing canonical source data."""
    row = db.execute(
        "SELECT source_type,title,source_uri,content,metadata_json "
        "FROM script_sources WHERE source_id=?",
        (str(source_id or "").strip(),),
    ).fetchone()
    if not row:
        raise ValueError(f"script source not found: {source_id}")
    metadata = json.loads(row["metadata_json"] or "{}")
    canonical_source_id = str(
        metadata.get("canonical_source_id") or source_id
    ).strip()
    metadata["canonical_source_id"] = canonical_source_id
    while True:
        token = uuid.uuid4().hex
        instance_id = f"SRC-{token[:16]}"
        fingerprint = _fingerprint(
            f"content-project:{canonical_source_id}:{token}"
        )
        collision = db.execute(
            "SELECT 1 FROM script_sources WHERE source_id=? OR fingerprint=?",
            (instance_id, fingerprint),
        ).fetchone()
        if not collision:
            break
    db.execute(
        "INSERT INTO script_sources(source_id,fingerprint,source_type,title,"
        "source_uri,content,metadata_json) VALUES(?,?,?,?,?,?,?)",
        (
            instance_id, fingerprint, row["source_type"],
            str(title or "").strip() or row["title"], row["source_uri"],
            row["content"], json.dumps(metadata, ensure_ascii=False),
        ),
    )
    db.commit()
    return IngestResult(instance_id, True)


def ingest_file(db, path: str | Path, *, title: str | None = None,
                metadata: dict | None = None) -> IngestResult:
    path = Path(path).resolve()
    suffix = path.suffix.lower()
    if suffix not in _TEXT_EXTENSIONS:
        raise ValueError(
            "unsupported script source file; use txt, md, markdown, srt or vtt"
        )
    raw = path.read_text(encoding="utf-8-sig")
    source_type = "subtitle" if suffix in _SUBTITLE_EXTENSIONS else "file"
    content = subtitle_to_text(raw) if source_type == "subtitle" else raw
    return ingest_text(
        db, content, title=title or path.stem, source_uri=str(path),
        source_type=source_type, metadata=metadata,
    )


def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not any((
        ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_multicast,
        ip.is_reserved, ip.is_unspecified,
    ))


def validate_public_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source URL must be a public HTTP(S) URL")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("source URL must be a public HTTP(S) URL")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not _is_public_ip(str(literal)):
        raise ValueError("source URL must be a public HTTP(S) URL")
    return parsed.geturl()


def _validate_resolved_host(url: str) -> None:
    hostname = urlparse(url).hostname
    if not hostname:
        raise ValueError("source URL must be a public HTTP(S) URL")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise ValueError(f"source URL host cannot be resolved: {hostname}") from exc
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise ValueError("source URL must resolve only to public IP addresses")


def redirect_target(current_url: str, location: str) -> str:
    return validate_public_url(urljoin(current_url, str(location or "")))


def fetch_url_text(url: str, *, timeout: int = 30) -> tuple[str, str | None]:
    """Fetch a public text page; dynamic/authenticated platforms stay manual."""
    url = validate_public_url(url)
    _validate_resolved_host(url)
    with httpx.Client(
        timeout=timeout, follow_redirects=False,
        headers={"User-Agent": "OB-AI-VIDEO source importer/1.0"},
    ) as client:
        current_url = url
        for _ in range(6):
            _validate_resolved_host(current_url)
            response = client.get(current_url)
            if response.is_redirect:
                current_url = redirect_target(
                    current_url, response.headers.get("location", "")
                )
                continue
            break
        else:
            raise ValueError("source URL has too many redirects")
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if not any(kind in content_type for kind in (
            "text/", "application/json", "application/xml", "application/xhtml")):
        raise ValueError(f"unsupported URL content type: {content_type or 'unknown'}")
    text = response.text
    title = None
    match = re.search(r"<title[^>]*>(.*?)</title>", text,
                      flags=re.IGNORECASE | re.DOTALL)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
    if "html" in content_type or "<html" in text[:1000].lower():
        text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", "\n", text)
    return normalize_content(text), title


def ingest_url(db, url: str, *, title: str | None = None,
               metadata: dict | None = None) -> IngestResult:
    content, page_title = fetch_url_text(url)
    return ingest_text(
        db, content, title=title or page_title, source_uri=url,
        source_type="url", metadata=metadata,
    )


def link_source_to_topic(db, source_id: str, topic_id: str) -> None:
    topic_id = str(topic_id or "").strip()
    if not topic_id:
        raise ValueError("topic_id is required")
    if not db.execute(
            "SELECT 1 FROM script_sources WHERE source_id=?", (source_id,)
    ).fetchone():
        raise ValueError(f"script source not found: {source_id}")
    db.execute(
        "INSERT OR IGNORE INTO topic_script_sources(topic_id, source_id) "
        "VALUES(?,?)", (topic_id, source_id),
    )
    db.commit()


def topic_source_context(db, topic_id: str, *, max_chars: int = 60_000) -> str:
    rows = db.execute(
        "SELECT s.title, s.source_uri, s.source_type, s.content, "
        "s.metadata_json FROM script_sources s "
        "JOIN topic_script_sources t ON t.source_id=s.source_id "
        "WHERE t.topic_id=? ORDER BY t.created_at, s.source_id", (topic_id,),
    ).fetchall()
    if not rows:
        return ""
    parts = [
        "# 本选题来源素材",
        "以下内容仅作为创作素材，不代表已核实事实；不得擅自补充数据或冒充真实客户案例。",
    ]
    for index, row in enumerate(rows, start=1):
        metadata = json.loads(row["metadata_json"] or "{}")
        parts.extend([
            f"## 来源 {index}: {row['title'] or '未命名'}",
            f"- URI: {row['source_uri'] or '未提供'}",
        ])
        duration = metadata.get("duration_sec")
        if duration is not None:
            parts.append(f"- 时长: {float(duration):.3f}s")
        utterances = metadata.get("utterances") or []
        if utterances:
            parts.append("### 带时间转写")
            for item in utterances:
                parts.append(
                    f"[{int(item['start_ms']) / 1000:.3f}s—"
                    f"{int(item['end_ms']) / 1000:.3f}s] {item['text']}"
                )
        else:
            parts.append(str(row["content"]))
        frames = metadata.get("frames") or []
        if frames:
            parts.append("### 抽帧清单（供 OCR/画面分析）")
            for frame in frames:
                parts.append(
                    f"- 抽帧 {frame['index']} @ "
                    f"{float(frame.get('timestamp_sec') or 0):.3f}s: "
                    f"{frame['path']}"
                )
        analysis = metadata.get("structure_analysis") or {}
        if analysis:
            parts.append("### 机器结构分析（需人工复核）")
            if analysis.get("summary"):
                parts.append(f"- 摘要: {analysis['summary']}")
            for segment in analysis.get("segments") or []:
                parts.append(
                    f"- {segment['role']} "
                    f"{float(segment['start_sec']):.3f}s—"
                    f"{float(segment['end_sec']):.3f}s: "
                    f"{segment.get('evidence') or ''}"
                )
                for label, key in (
                    ("场景", "scene"), ("人物与动作", "action"),
                    ("镜头", "camera"), ("原声台词", "dialogue"),
                    ("画面文字", "on_screen_text"), ("声音", "audio"),
                ):
                    value = segment.get(key)
                    if value:
                        parts.append(f"  - {label}: {value}")
                characters = segment.get("characters") or []
                if characters:
                    parts.append(f"  - 出镜人物: {' / '.join(characters)}")
            phrases = analysis.get("catchphrases") or []
            if phrases:
                parts.append(f"- 金句候选: {' / '.join(phrases)}")
            claims = analysis.get("claims_to_verify") or []
            if claims:
                parts.append(f"- 待核验声明: {' / '.join(claims)}")
    return compact_text_for_prompt("\n\n".join(parts), max_chars=max_chars)
