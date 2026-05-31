"""
Unofficial BO3.gg REST API wrapper (starter)
Python 3.9 compatible.

Run:
  python3 -m pip install fastapi uvicorn httpx
  python3 main.py

Examples:
  curl "http://127.0.0.1:3002/v2/match?q=finished"
  curl "http://127.0.0.1:3002/v2/match?q=current"
  curl "http://127.0.0.1:3002/v2/match/details?url=https://bo3.gg/matches/atreides-vs-g2-ares-31-05-2026"
"""

import asyncio
import html
import os
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query

BASE_URL = "https://bo3.gg"
API_PORT = int(os.getenv("BO3API_PORT", "3002"))
DEFAULT_TIMEOUT = float(os.getenv("BO3API_TIMEOUT", "20"))
CACHE_TTL_SECONDS = int(os.getenv("BO3API_CACHE_TTL", "30"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

MAP_NAMES = (
    "Dust II",
    "Mirage",
    "Inferno",
    "Nuke",
    "Train",
    "Ancient",
    "Anubis",
    "Vertigo",
    "Overpass",
    "Cache",
    "Cobblestone",
)


class AnchorExtractor(HTMLParser):
    """Small dependency-free anchor extractor.

    BO3.gg pages are server-rendered enough that a simple HTML parser can pull
    public links and text without Playwright. This intentionally avoids JS.
    """

    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self._stack: List[Dict[str, Any]] = []
        self.anchors: List[Dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = dict(attrs)
        self._stack.append({"href": attrs_dict.get("href") or "", "text": []})

    def handle_data(self, data: str) -> None:
        if not self._stack:
            return
        for item in self._stack:
            item["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._stack:
            return
        item = self._stack.pop()
        href = (item.get("href") or "").strip()
        text = collapse_ws(" ".join(item.get("text") or []))
        if href and text:
            self.anchors.append({"href": href, "text": text})


class TextExtractor(HTMLParser):
    """Dependency-free visible text extractor."""

    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self._skip_depth = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif tag in {"br", "p", "div", "section", "li", "tr", "td", "th", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"p", "div", "section", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        data = data.strip()
        if data:
            self.parts.append(data)

    def text(self) -> str:
        lines = [collapse_ws(x) for x in "\n".join(self.parts).splitlines()]
        lines = [x for x in lines if x]
        return "\n".join(lines)


@dataclass
class CacheEntry:
    ts: float
    value: str


_cache: Dict[str, CacheEntry] = {}
_client: Optional[httpx.AsyncClient] = None


def collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<script\b[^>]*>.*?</script>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<style\b[^>]*>.*?</style>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return collapse_ws(html.unescape(fragment))


def normalize_url(url_or_path: str) -> str:
    if not url_or_path:
        raise ValueError("empty URL/path")
    full_url = urljoin(BASE_URL, url_or_path)
    parsed = urlparse(full_url)
    if parsed.netloc and parsed.netloc not in {"bo3.gg", "www.bo3.gg"}:
        raise ValueError("only bo3.gg URLs are allowed")
    return full_url


def extract_anchors(raw_html: str) -> List[Dict[str, str]]:
    parser = AnchorExtractor()
    parser.feed(raw_html)
    out: List[Dict[str, str]] = []
    for item in parser.anchors:
        href = item.get("href", "")
        text = item.get("text", "")
        if href.startswith("/"):
            href = urljoin(BASE_URL, href)
        out.append({"href": href, "text": text})
    return out


def extract_visible_text(raw_html: str) -> str:
    parser = TextExtractor()
    parser.feed(raw_html)
    return parser.text()


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=DEFAULT_TIMEOUT, follow_redirects=True)
    return _client


async def fetch_html(url_or_path: str, ttl: int = CACHE_TTL_SECONDS) -> str:
    url = normalize_url(url_or_path)
    now = time.time()
    cached = _cache.get(url)
    if cached and now - cached.ts <= ttl:
        return cached.value

    client = await get_client()
    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            resp = await client.get(url)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * attempt
                await asyncio.sleep(min(delay, 10.0))
                continue
            resp.raise_for_status()
            _cache[url] = CacheEntry(time.time(), resp.text)
            return resp.text
        except Exception as exc:
            last_exc = exc
            if attempt < 3:
                await asyncio.sleep(0.5 * attempt)

    raise HTTPException(status_code=502, detail="BO3.gg fetch failed: %s" % (last_exc,))


def clean_team_text(value: str) -> str:
    value = collapse_ws(value)
    value = re.sub(r"^(Live|Ended)\s+", "", value, flags=re.I)
    value = re.sub(r"^\d{1,2}:\d{2}\s+", "", value)
    value = re.sub(r"^[A-Z][a-z]{2}\s+\d{1,2},\s*\d{1,2}:\d{2}\s+", "", value)
    value = re.sub(r"\bBo[135]\b", "", value, flags=re.I)
    return collapse_ws(value)


def parse_match_anchor(text: str, href: str, status_hint: str) -> Optional[Dict[str, Any]]:
    raw = collapse_ws(text)
    if not raw:
        return None

    href_path = urlparse(href).path
    if not href_path.startswith("/matches/"):
        return None
    if href_path.rstrip("/") in {"/matches", "/matches/current", "/matches/finished"}:
        return None

    lower = raw.lower()
    has_score = bool(re.search(r"\b\d+\s*-\s*\d+\b", raw))
    has_status = lower.startswith("live") or lower.startswith("ended")
    has_time = bool(re.match(r"^(?:[A-Z][a-z]{2}\s+\d{1,2},\s*)?\d{1,2}:\d{2}\b", raw))
    if not (has_score or has_status or has_time):
        return None

    started_at = ""
    m_time = re.match(r"^((?:[A-Z][a-z]{2}\s+\d{1,2},\s*)?\d{1,2}:\d{2})\b", raw)
    if m_time:
        started_at = m_time.group(1)

    status = status_hint
    if lower.startswith("live"):
        status = "live"
    elif lower.startswith("ended") or status_hint == "finished":
        status = "finished"

    score1: Optional[int] = None
    score2: Optional[int] = None
    team1 = ""
    team2 = ""

    score_match = re.search(r"\b(\d+)\s*-\s*(\d+)\b", raw)
    if score_match:
        score1 = int(score_match.group(1))
        score2 = int(score_match.group(2))
        left = clean_team_text(raw[: score_match.start()])
        right = clean_team_text(raw[score_match.end() :])
        # BO3 list rows sometimes append a one-digit prediction/tier marker.
        right = re.sub(r"\s+\d+$", "", right).strip()

        # Best-effort split. BO3 detail endpoint is the source of truth.
        if right:
            team2 = right
            team1 = left
        else:
            parts = left.split()
            if len(parts) >= 2:
                # fallback only: cannot always know multi-word team boundaries from list row text
                midpoint = len(parts) // 2
                team1 = " ".join(parts[:midpoint])
                team2 = " ".join(parts[midpoint:])
            else:
                team1 = left
    else:
        no_time = clean_team_text(raw)
        team1 = no_time

    return {
        "raw_text": raw,
        "status": status,
        "time": started_at,
        "team1": team1,
        "team2": team2,
        "score1": score1,
        "score2": score2,
        "url": href,
    }


def parse_match_list(raw_html: str, status_hint: str) -> Dict[str, Any]:
    seen = set()
    segments: List[Dict[str, Any]] = []
    for anchor in extract_anchors(raw_html):
        href = anchor["href"]
        text = anchor["text"]
        key = (urlparse(href).path, text)
        if key in seen:
            continue
        seen.add(key)
        item = parse_match_anchor(text, href, status_hint)
        if item:
            segments.append(item)

    return {"status": 200, "segments": segments, "count": len(segments)}


def parse_title(raw_html: str, visible: str) -> str:
    h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", raw_html, flags=re.I | re.S)
    if h1:
        return strip_tags(h1.group(1))
    title = re.search(r"<title\b[^>]*>(.*?)</title>", raw_html, flags=re.I | re.S)
    if title:
        return strip_tags(title.group(1))
    for line in visible.splitlines():
        if " vs " in line:
            return line
    return ""


def parse_teams_from_title(title: str) -> Tuple[str, str, str]:
    # Example: "Atreides vs G2 Ares at NODWIN Clutch Series 9 Play-In"
    m = re.search(r"(.+?)\s+vs\s+(.+?)(?:\s+at\s+(.+))?$", title, flags=re.I)
    if not m:
        return "", "", ""
    return collapse_ws(m.group(1)), collapse_ws(m.group(2)), collapse_ws(m.group(3) or "")


def parse_series_score(visible: str, team1: str, team2: str) -> Dict[str, Any]:
    if not team1 or not team2:
        return {"score1": None, "score2": None, "winner": ""}
    pattern = re.compile(
        re.escape(team1) + r"\s+(\d+)\s*-\s*(\d+)\s+" + re.escape(team2),
        flags=re.I | re.S,
    )
    m = pattern.search(visible.replace("\n", " "))
    if not m:
        # Try reversed text layout.
        pattern2 = re.compile(
            re.escape(team2) + r"\s+(\d+)\s*-\s*(\d+)\s+" + re.escape(team1),
            flags=re.I | re.S,
        )
        m2 = pattern2.search(visible.replace("\n", " "))
        if not m2:
            return {"score1": None, "score2": None, "winner": ""}
        s2 = int(m2.group(1))
        s1 = int(m2.group(2))
    else:
        s1 = int(m.group(1))
        s2 = int(m.group(2))

    winner = team1 if s1 > s2 else team2 if s2 > s1 else "draw"
    return {"score1": s1, "score2": s2, "winner": winner}


def parse_map_scores(visible: str, team1: str, team2: str) -> List[Dict[str, Any]]:
    text = visible.replace("\n", " ")
    maps: List[Dict[str, Any]] = []
    map_alt = "|".join(re.escape(x) for x in MAP_NAMES)
    for m in re.finditer(r"\b(" + map_alt + r")\s+(\d+)\s*-\s*(\d+)\b", text, flags=re.I):
        map_name = collapse_ws(m.group(1))
        s1 = int(m.group(2))
        s2 = int(m.group(3))
        winner = team1 if team1 and s1 > s2 else team2 if team2 and s2 > s1 else "draw"
        maps.append({"map": map_name, "score1": s1, "score2": s2, "winner": winner})

    # Remove duplicates caused by repeated page sections.
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in maps:
        key = (item["map"].lower(), item["score1"], item["score2"])
        if key not in seen:
            deduped.append(item)
            seen.add(key)
    return deduped


def parse_picks_bans(visible: str) -> List[Dict[str, str]]:
    marker = re.search(r"Picks\s*&\s*bans(.+?)(?:Historical|Lineups|Team Form|Records|$)", visible, flags=re.I | re.S)
    if not marker:
        return []
    chunk = marker.group(1).replace("\n", " ")
    map_alt = "|".join(re.escape(x) for x in MAP_NAMES)
    out: List[Dict[str, str]] = []
    for m in re.finditer(r"\b(" + map_alt + r")\s+(ban|pick|decider)\b", chunk, flags=re.I):
        out.append({"map": collapse_ws(m.group(1)), "action": m.group(2).lower()})
    return out


def parse_streams(visible: str) -> List[str]:
    marker = re.search(r"Stream\s+(.+?)(?:Team Form|Teams advantage|Lineups|Picks|$)", visible, flags=re.I | re.S)
    if not marker:
        return []
    chunk = marker.group(1)
    # Keep textual channel names, drop pure viewer counts.
    out = []
    for line in chunk.splitlines():
        line = collapse_ws(line)
        if line and not re.match(r"^\d+$", line):
            out.append(line)
    return out[:10]


def parse_match_detail(raw_html: str, source_url: str) -> Dict[str, Any]:
    visible = extract_visible_text(raw_html)
    title = parse_title(raw_html, visible)
    team1, team2, tournament = parse_teams_from_title(title)
    series = parse_series_score(visible, team1, team2)
    maps = parse_map_scores(visible, team1, team2)
    picks_bans = parse_picks_bans(visible)
    streams = parse_streams(visible)

    status = "unknown"
    if re.search(r"\bLive\b", visible[:1000], flags=re.I):
        status = "live"
    if re.search(r"\bEnded\b", visible[:1000], flags=re.I):
        status = "finished"

    return {
        "status": 200,
        "segments": [
            {
                "title": title,
                "url": source_url,
                "status": status,
                "tournament": tournament,
                "teams": [
                    {"name": team1, "score": series["score1"]},
                    {"name": team2, "score": series["score2"]},
                ],
                "winner": series["winner"],
                "maps": maps,
                "picks_bans": picks_bans,
                "streams": streams,
            }
        ],
    }


app = FastAPI(
    title="bo3ggapi",
    description="Unofficial REST API wrapper for public BO3.gg CS2 pages.",
    docs_url="/",
    redoc_url=None,
)


@app.on_event("shutdown")
async def shutdown() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()


@app.get("/version", tags=["Meta"])
def version() -> Dict[str, str]:
    return {"version": "0.1.0", "default_api": "v2", "source": "bo3.gg"}


@app.get("/v2/health", tags=["Meta"])
async def health() -> Dict[str, Any]:
    try:
        raw = await fetch_html("/matches/current", ttl=10)
        return {"status": "success", "upstream": "ok", "bytes": len(raw)}
    except Exception as exc:
        return {"status": "error", "upstream": "failed", "error": str(exc)}


@app.get("/v2/match", tags=["Matches"])
async def match(
    q: str = Query(..., description="current/live/schedule/upcoming/finished/results"),
) -> Dict[str, Any]:
    q_norm = q.lower().strip()
    if q_norm in {"current", "live", "schedule", "upcoming"}:
        path = "/matches/current"
        hint = "current"
        ttl = 20
    elif q_norm in {"finished", "results"}:
        path = "/matches/finished"
        hint = "finished"
        ttl = 60
    else:
        raise HTTPException(status_code=400, detail="q must be one of current/live/schedule/upcoming/finished/results")

    raw = await fetch_html(path, ttl=ttl)
    return {"status": "success", "data": parse_match_list(raw, hint)}


@app.get("/v2/match/details", tags=["Matches"])
async def match_details(
    url: Optional[str] = Query(None, description="Full BO3.gg match URL"),
    path: Optional[str] = Query(None, description="BO3.gg match path, e.g. /matches/team-a-vs-team-b-31-05-2026"),
) -> Dict[str, Any]:
    target = url or path
    if not target:
        raise HTTPException(status_code=400, detail="provide url= or path=")
    try:
        full_url = normalize_url(target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not urlparse(full_url).path.startswith("/matches/"):
        raise HTTPException(status_code=400, detail="match details URL must be under /matches/")

    raw = await fetch_html(full_url, ttl=60)
    return {"status": "success", "data": parse_match_detail(raw, full_url)}


@app.get("/v2/search", tags=["Search"])
async def search(q: str = Query(..., min_length=2), source: str = Query("finished")) -> Dict[str, Any]:
    source_norm = source.lower().strip()
    path = "/matches/current" if source_norm in {"current", "live", "upcoming"} else "/matches/finished"
    raw = await fetch_html(path, ttl=30)
    data = parse_match_list(raw, "current" if path.endswith("current") else "finished")
    q_fold = q.casefold()
    matches = [x for x in data["segments"] if q_fold in x.get("raw_text", "").casefold()]
    return {"status": "success", "data": {"status": 200, "segments": matches, "count": len(matches)}}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=API_PORT)
