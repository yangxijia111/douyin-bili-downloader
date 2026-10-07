"""Offline extraction of URLs from app share text for both entry points."""

import re
from typing import List

_END = r"[^\s<>\"'（）)【】\]】，。、；！？]*"
_URL_RE = re.compile(r"https?://" + _END, re.IGNORECASE)
_BARE_RE = re.compile(
    r"(?<![0-9A-Za-z._:/@-])"
    r"((?:[a-z0-9-]+\.)*(?:douyin\.com|iesdouyin\.com|webcast\.amemv\.com"
    r"|bilibili\.com|b23\.tv|acg\.tv|iqiyi\.com|iq\.com|v\.qq\.com|video\.qq\.com"
    r"|youku\.com|mgtv\.com|kuaishou\.com|gifshow\.com|chenzhongtech\.com"
    r"|ixigua\.com|toutiao\.com|weibo\.com|weibo\.cn|xiaohongshu\.com|xhslink\.com"
    r"|channels\.weixin\.qq\.com|weixin\.qq\.com)/" + _END + r")",
    re.IGNORECASE,
)
_BVID_RE = re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])")
_AVID_RE = re.compile(r"^av\d+$", re.IGNORECASE)
_TRAILING_JUNK = ".,;:!?)]}>）】、，。；：！？"


def extract_urls_from_text(raw: str, deduplicate: bool = True) -> List[str]:
    """Extract every link in source order, without network requests."""
    text = (raw or "").strip()
    matches = [(m.start(), m.end(), m.group(0)) for m in _URL_RE.finditer(text)]
    for pattern, prefix in ((_BARE_RE, "https://"), (_BVID_RE, "")):
        for match in pattern.finditer(text):
            # A domain or BV ID inside a full URL is part of that URL.
            if any(start <= match.start() < end for start, end, _ in matches):
                continue
            matches.append((match.start(), match.end(), prefix + match.group(1)))
    if not matches and _AVID_RE.fullmatch(text):
        return [text]
    urls = [value.rstrip(_TRAILING_JUNK) for _, _, value in sorted(matches)]
    return list(dict.fromkeys(urls)) if deduplicate else urls


def extract_url_from_text(raw: str) -> str:
    """Compatibility helper for existing single-link callers."""
    urls = extract_urls_from_text(raw)
    return urls[0] if urls else (raw or "").strip()


def expand_link_inputs(inputs: List[str]) -> List[str]:
    """Expand CLI/config entries while retaining invalid inputs for diagnostics."""
    urls = []
    for raw in inputs:
        urls.extend(extract_urls_from_text(raw) or ([raw.strip()] if raw.strip() else []))
    return list(dict.fromkeys(urls))
