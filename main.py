"""
Unofficial BO3.gg REST API wrapper
Python 3.9 compatible. Multi-game build for CS2, Valorant, R6S, Dota2, LoL, and MLBB.

Vercel/GitHub layout:
  main.py
  app.py          -> from main import app
  requirements.txt
  vercel.json

Local run:
  python3 -m pip install -r requirements.txt
  python3 main.py

Examples:
  curl "http://127.0.0.1:3002/v2/match?game=cs2&q=finished"
  curl "http://127.0.0.1:3002/v2/match?game=valorant&q=current"
  curl "http://127.0.0.1:3002/v2/match/all?q=finished"
  curl "http://127.0.0.1:3002/v2/match/details?url=https://bo3.gg/matches/gentle-mates-cs-vs-team-nemesis-cs-31-05-2026"
"""

import asyncio
import html
import json
import os
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

BASE_URL = "https://bo3.gg"
API_PORT = int(os.getenv("BO3API_PORT", "3002"))
DEFAULT_TIMEOUT = float(os.getenv("BO3API_TIMEOUT", "20"))
CACHE_TTL_SECONDS = int(os.getenv("BO3API_CACHE_TTL", "30"))
DEBUG_FETCH_CHARS = int(os.getenv("BO3API_DEBUG_FETCH_CHARS", "1500"))

# BO3.gg separates games by URL namespace. Root /matches is CS2 only.
GAME_PREFIXES = {
    "cs2": "",
    "cs": "",
    "counterstrike": "",
    "counter-strike": "",
    "valorant": "/valorant",
    "val": "/valorant",
    "r6s": "/r6siege",
    "r6": "/r6siege",
    "rainbow6": "/r6siege",
    "rainbow-six": "/r6siege",
    "rainbowsix": "/r6siege",
    "r6siege": "/r6siege",
    "dota2": "/dota2",
    "dota": "/dota2",
    "lol": "/lol",
    "league": "/lol",
    "leagueoflegends": "/lol",
    "league-of-legends": "/lol",
    "mlbb": "/mlbb",
    "mobilelegends": "/mlbb",
    "mobile-legends": "/mlbb",
}

CANONICAL_GAMES = {
    "cs2": {"slug": "cs2", "name": "CS2", "prefix": ""},
    "valorant": {"slug": "valorant", "name": "Valorant", "prefix": "/valorant"},
    "r6s": {"slug": "r6s", "name": "Rainbow Six Siege", "prefix": "/r6siege"},
    "dota2": {"slug": "dota2", "name": "Dota 2", "prefix": "/dota2"},
    "lol": {"slug": "lol", "name": "League of Legends", "prefix": "/lol"},
    "mlbb": {"slug": "mlbb", "name": "Mobile Legends: Bang Bang", "prefix": "/mlbb"},
}

PREFIX_TO_GAME = {"": "cs2", "/valorant": "valorant", "/r6siege": "r6s", "/dota2": "dota2", "/lol": "lol", "/mlbb": "mlbb"}
GAME_PREFIX_PATTERN = r"(?:valorant|r6siege|dota2|lol|mlbb)"

# Do not request br/zstd. Vercel's Python runtime + httpx can behave differently
# depending on optional decoder packages. gzip/deflate are safe everywhere.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

# BO3.gg/Nuxt can return an empty client-side shell to normal server-side
# HTTP clients. Search crawlers often receive prerendered HTML. Try those UAs
# before giving up, otherwise Vercel gets 0 visible chars / 0 anchors.
HEADER_PROFILES = [
    ("desktop", HEADERS),
    (
        "googlebot",
        dict(
            HEADERS,
            **{
                "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                "X-Forwarded-For": "66.249.66.1",
            },
        ),
    ),
    (
        "bingbot",
        dict(
            HEADERS,
            **{
                "User-Agent": "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
                "X-Forwarded-For": "40.77.167.1",
            },
        ),
    ),
    (
        "facebook",
        dict(
            HEADERS,
            **{
                "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            },
        ),
    ),
]

MAP_NAMES = (
    "Dust II",
    "Dust 2",
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

NOISE_LINES = {
    "0 comments",
    "comments",
    "full stats",
    "overview",
    "performance",
    "aim",
    "grenades",
    "devices",
    "economy",
    "full match winner",
    "scoreboard",
    "k",
    "d",
    "a",
    "+/-",
    "adr",
    "od",
    "mk",
    "maps score",
    "score",
    "form",
    "time",
    "match",
    "prediction",
    "tournament",
    "data",
    "pred.",
    "t",
}

STATUS_WORDS = {"live", "ended", "scheduled", "postponed", "cancelled"}


class AnchorExtractor(HTMLParser):
    """Dependency-free anchor extractor."""

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
        if href:
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
        elif tag in {
            "br",
            "p",
            "div",
            "section",
            "article",
            "li",
            "tr",
            "td",
            "th",
            "h1",
            "h2",
            "h3",
            "h4",
            "header",
            "footer",
            "main",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        data = clean_text(data)
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


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\xa0", " ")
    value = value.replace("\u00a0", " ")
    value = value.replace("–", "-")
    value = value.replace("—", "-")
    value = value.replace("−", "-")
    return value.strip()


def collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).strip()


def norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", collapse_ws(value).casefold())


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<script\b[^>]*>.*?</script>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<style\b[^>]*>.*?</style>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<svg\b[^>]*>.*?</svg>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return collapse_ws(fragment)


def normalize_game(game: str) -> str:
    key = re.sub(r"[^a-z0-9-]+", "", (game or "cs2").strip().lower())
    if not key:
        key = "cs2"
    if key not in GAME_PREFIXES:
        raise ValueError("unsupported game '%s'; use cs2, valorant, r6s, dota2, lol, or mlbb" % game)
    prefix = GAME_PREFIXES[key]
    return PREFIX_TO_GAME.get(prefix, "cs2")


def game_prefix(game: str) -> str:
    canonical = normalize_game(game)
    return CANONICAL_GAMES[canonical]["prefix"]


def game_from_path(path: str) -> str:
    path = path or ""
    m = re.match(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?(" + GAME_PREFIX_PATTERN + r")(?=/|$)", path, flags=re.I)
    if m:
        return PREFIX_TO_GAME.get("/" + m.group(1).lower(), "cs2")
    return "cs2"


def match_list_path(game: str, q_norm: str) -> Tuple[str, str, int]:
    prefix = game_prefix(game)
    if q_norm in {"current", "live", "schedule", "upcoming"}:
        return prefix + "/matches/current", "current", 20
    if q_norm in {"finished", "results"}:
        return prefix + "/matches/finished", "finished", 60
    raise ValueError("q must be one of current/live/schedule/upcoming/finished/results")


def normalize_url(url_or_path: str) -> str:
    if not url_or_path:
        raise ValueError("empty URL/path")
    full_url = urljoin(BASE_URL, url_or_path)
    parsed = urlparse(full_url)
    if parsed.netloc and parsed.netloc not in {"bo3.gg", "www.bo3.gg"}:
        raise ValueError("only bo3.gg URLs are allowed")
    return full_url


def abs_bo3_url(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return href
    if href.startswith("/"):
        return urljoin(BASE_URL, href)
    return href


def canonical_match_path(path: str) -> str:
    path = path or ""
    # BO3 can prepend language prefixes, e.g. /en/valorant/matches/...
    m = re.match(r"^/[a-z]{2}(?:-[a-z]{2})?(/(?:(?:" + GAME_PREFIX_PATTERN + r")/)?matches/.*)$", path, flags=re.I)
    if m:
        return m.group(1)
    return path


def is_match_collection_path(path: str) -> bool:
    path = canonical_match_path(path).rstrip("/").lower()
    return bool(re.fullmatch(r"/(?:" + GAME_PREFIX_PATTERN + r"/)?matches(?:/(?:current|finished))?", path))


def is_match_detail_path(path: str) -> bool:
    path = canonical_match_path(path).rstrip("/")
    if is_match_collection_path(path):
        return False
    return bool(re.match(r"^/(?:" + GAME_PREFIX_PATTERN + r"/)?matches/[^/]+$", path, flags=re.I))


def extract_anchors_htmlparser(raw_html: str) -> List[Dict[str, str]]:
    parser = AnchorExtractor()
    parser.feed(raw_html)
    out: List[Dict[str, str]] = []
    for item in parser.anchors:
        href = abs_bo3_url(item.get("href", ""))
        text = collapse_ws(item.get("text", ""))
        if href:
            out.append({"href": href, "text": text})
    return out


def extract_anchors_regex(raw_html: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    # This fallback is intentionally simple and robust for SSR anchor blocks.
    for m in re.finditer(r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>", raw_html, flags=re.I | re.S):
        attrs = m.group("attrs") or ""
        href_m = re.search(r"\bhref\s*=\s*(['\"])(.*?)\1", attrs, flags=re.I | re.S)
        if not href_m:
            href_m = re.search(r"\bhref\s*=\s*([^\s>]+)", attrs, flags=re.I | re.S)
        if not href_m:
            continue
        href = href_m.group(2 if href_m.lastindex and href_m.lastindex >= 2 else 1)
        href = abs_bo3_url(html.unescape(href))
        text = strip_tags(m.group("body") or "")
        if href:
            out.append({"href": href, "text": text})
    return out


def extract_json_ld_anchors(raw_html: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in re.finditer(
        r"<script\b[^>]*type\s*=\s*(['\"])application/ld\+json\1[^>]*>(.*?)</script>",
        raw_html,
        flags=re.I | re.S,
    ):
        blob = html.unescape(m.group(2) or "").strip()
        try:
            data = json.loads(blob)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                url = item.get("url") or item.get("@id") or ""
                name = item.get("name") or item.get("headline") or ""
                if isinstance(url, str) and "/matches/" in url:
                    out.append({"href": abs_bo3_url(url), "text": collapse_ws(str(name))})
                for value in item.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
    return out


def extract_anchors(raw_html: str) -> List[Dict[str, str]]:
    all_items = []
    all_items.extend(extract_anchors_htmlparser(raw_html))
    all_items.extend(extract_anchors_regex(raw_html))
    all_items.extend(extract_json_ld_anchors(raw_html))

    deduped: List[Dict[str, str]] = []
    seen = set()
    for item in all_items:
        href = abs_bo3_url(item.get("href", ""))
        text = collapse_ws(item.get("text", ""))
        key = (href, text)
        if href and key not in seen:
            deduped.append({"href": href, "text": text})
            seen.add(key)
    return deduped


def extract_visible_text(raw_html: str) -> str:
    parser = TextExtractor()
    parser.feed(raw_html)
    text = parser.text()
    if text:
        return text
    return strip_tags(raw_html).replace(" | ", "\n")


def visible_lines(visible: str) -> List[str]:
    lines = []
    for raw in visible.splitlines():
        line = collapse_ws(raw)
        if line:
            lines.append(line)
    return lines


def first_meta_content(raw_html: str, names: Iterable[str]) -> str:
    for name in names:
        # <meta property="og:title" content="...">
        pat1 = (
            r"<meta\b(?=[^>]*(?:property|name)\s*=\s*(['\"])"
            + re.escape(name)
            + r"\1)(?=[^>]*content\s*=\s*(['\"])(.*?)\2)[^>]*>"
        )
        m = re.search(pat1, raw_html, flags=re.I | re.S)
        if m:
            return collapse_ws(m.group(3))
    return ""


def parse_html_title(raw_html: str) -> str:
    h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", raw_html, flags=re.I | re.S)
    if h1:
        title = strip_tags(h1.group(1))
        if title:
            return title

    meta_title = first_meta_content(raw_html, ["og:title", "twitter:title"])
    if meta_title:
        return meta_title

    title = re.search(r"<title\b[^>]*>(.*?)</title>", raw_html, flags=re.I | re.S)
    if title:
        return strip_tags(title.group(1))

    return ""


def cleanup_page_title(title: str) -> str:
    title = collapse_ws(title)
    title = re.sub(r"\s+-\s+(?:CS2|Valorant|R6SIEGE|R6|Dota2?|LoL|MLBB)\s+Match.*$", "", title, flags=re.I)
    title = re.sub(r"\s+\|\s+BO3\.gg.*$", "", title, flags=re.I)
    title = re.sub(r"\s+\|\s+bo3\.gg.*$", "", title, flags=re.I)
    return collapse_ws(title)


def slug_to_name(value: str) -> str:
    value = re.sub(r"[-_]+", " ", value or "").strip()
    words = []
    for word in value.split():
        lower = word.lower()
        if lower in {"cs", "cs2", "gg"}:
            words.append(lower.upper())
        elif lower in {"g2", "t1", "m80", "og", "big"}:
            words.append(lower.upper())
        elif lower in {"esports", "gaming"}:
            words.append(word.capitalize())
        else:
            words.append(word.capitalize())
    return collapse_ws(" ".join(words))


def teams_from_match_slug(path_or_url: str) -> Tuple[str, str]:
    parsed = urlparse(path_or_url)
    path = parsed.path if parsed.scheme or parsed.netloc else path_or_url
    path = canonical_match_path(path)
    slug = path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d{1,2}-\d{1,2}-\d{4}$", "", slug)
    if "-vs-" not in slug:
        return "", ""
    left, right = slug.split("-vs-", 1)
    return slug_to_name(left), slug_to_name(right)


def parse_title(raw_html: str, visible: str, source_url: str) -> str:
    title = cleanup_page_title(parse_html_title(raw_html))
    if title and " vs " in title:
        return title

    for line in visible_lines(visible):
        if " vs " in line and (" at " in line or line.lower().startswith("# ")):
            return cleanup_page_title(line.lstrip("# "))

    t1, t2 = teams_from_match_slug(source_url)
    if t1 and t2:
        return "%s vs %s" % (t1, t2)
    return title


def parse_teams_from_title(title: str) -> Tuple[str, str, str]:
    title = cleanup_page_title(title)
    m = re.search(r"(.+?)\s+vs\s+(.+?)(?:\s+at\s+(.+))?$", title, flags=re.I)
    if not m:
        return "", "", ""
    team1 = collapse_ws(m.group(1))
    team2 = collapse_ws(m.group(2))
    tournament = collapse_ws(m.group(3) or "")
    return team1, team2, tournament


def clean_team_text(value: str) -> str:
    value = collapse_ws(value)
    value = re.sub(r"^(Live|Ended)\s+", "", value, flags=re.I)
    value = re.sub(r"^[A-Z][a-z]{2}\s+\d{1,2},\s*\d{1,2}:\d{2}\s+", "", value)
    value = re.sub(r"^\d{1,2}:\d{2}\s+", "", value)
    value = re.sub(r"^Full\s+", "", value, flags=re.I)
    value = re.sub(r"^(Live|Ended)\s+", "", value, flags=re.I)
    value = re.sub(r"\bBo[1357]\b", "", value, flags=re.I)
    value = re.sub(r"\s+\d+\s+\d+\s*-\s*\d+\s*$", "", value)  # prediction tail
    value = re.sub(r"\s+\d+\s*$", "", value)  # prediction/team-pick tail
    return collapse_ws(value)


def parse_score(value: str) -> Optional[Tuple[int, int]]:
    m = re.search(r"\b(\d+)\s*-\s*(\d+)\b", clean_text(value))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def infer_status_from_text(value: str, hint: str) -> str:
    lower = value.lower()
    if re.search(r"\blive\b", lower):
        return "live"
    if re.search(r"\b(ended|full)\b", lower) or hint == "finished":
        return "finished"
    if hint in {"current", "live"}:
        return "live" if re.search(r"\blive\b", lower) else "upcoming"
    return hint or "unknown"


def parse_match_anchor(text: str, href: str, status_hint: str) -> Optional[Dict[str, Any]]:
    raw = collapse_ws(text)
    href = abs_bo3_url(href)
    href_path = canonical_match_path(urlparse(href).path)
    if not is_match_detail_path(href_path):
        return None

    slug_team1, slug_team2 = teams_from_match_slug(href_path)

    # Some BO3 anchor text can be empty if icons/images wrap the card. In that
    # case still return a useful shell from the slug, but list parsing below will
    # usually find text via regex anchors.
    if not raw and not (slug_team1 and slug_team2):
        return None

    started_at = ""
    m_time = re.search(r"\b((?:[A-Z][a-z]{2}\s+\d{1,2},\s*)?\d{1,2}:\d{2})\b", raw)
    if m_time:
        started_at = m_time.group(1)

    bo = ""
    m_bo = re.search(r"\bBo([1357])\b", raw, flags=re.I)
    if m_bo:
        bo = "bo" + m_bo.group(1)

    score1: Optional[int] = None
    score2: Optional[int] = None
    team1 = slug_team1
    team2 = slug_team2

    score_match = re.search(r"\b(\d+)\s*-\s*(\d+)\b", raw)
    if score_match:
        score1 = int(score_match.group(1))
        score2 = int(score_match.group(2))
        left = clean_team_text(raw[: score_match.start()])
        right = clean_team_text(raw[score_match.end() :])

        # Prefer names parsed from the visible row when they are usable, because
        # they preserve BO3 capitalization like UNiTY/FOKUS/ASTRAL.
        if left and right and not any(x.lower() in NOISE_LINES for x in [left, right]):
            team1 = left or team1
            team2 = right or team2
        elif left and not team1:
            team1 = left
        elif right and not team2:
            team2 = right
    else:
        # Upcoming/live rows sometimes have no series score yet. The slug is the
        # safest source for team names.
        cleaned = clean_team_text(raw)
        if cleaned and not (team1 and team2):
            if " vs " in cleaned:
                bits = re.split(r"\s+vs\s+", cleaned, maxsplit=1, flags=re.I)
                team1 = bits[0]
                team2 = bits[1]
            else:
                team1 = team1 or cleaned

    status = infer_status_from_text(raw, status_hint)
    winner = ""
    if score1 is not None and score2 is not None and team1 and team2:
        winner = team1 if score1 > score2 else team2 if score2 > score1 else "draw"

    return {
        "raw_text": raw,
        "status": status,
        "time": started_at,
        "bo": bo,
        "team1": team1,
        "team2": team2,
        "score1": score1,
        "score2": score2,
        "winner": winner,
        "url": href,
    }


def parse_match_list_from_anchors(raw_html: str, status_hint: str) -> List[Dict[str, Any]]:
    seen = set()
    segments: List[Dict[str, Any]] = []
    for anchor in extract_anchors(raw_html):
        href = anchor.get("href", "")
        text = anchor.get("text", "")
        item = parse_match_anchor(text, href, status_hint)
        if not item:
            continue
        key = canonical_match_path(urlparse(item["url"]).path)
        # Prefer the first populated item for each match URL.
        if key in seen:
            continue
        seen.add(key)
        segments.append(item)
    return segments


def parse_match_list_from_text(raw_html: str, status_hint: str) -> List[Dict[str, Any]]:
    """Fallback when anchor text is unavailable.

    Pull match URLs from href attributes and use the slug as team fallback.
    """
    segments: List[Dict[str, Any]] = []
    seen = set()
    for m in re.finditer(r"href\s*=\s*(['\"])(?P<href>[^'\"]*?/matches/[^'\"]+?)\1", raw_html, flags=re.I):
        href = abs_bo3_url(html.unescape(m.group("href")))
        path = canonical_match_path(urlparse(href).path)
        if not is_match_detail_path(path) or path in seen:
            continue
        seen.add(path)
        t1, t2 = teams_from_match_slug(path)
        if not (t1 and t2):
            continue
        segments.append(
            {
                "raw_text": "",
                "status": infer_status_from_text("", status_hint),
                "time": "",
                "bo": "",
                "team1": t1,
                "team2": t2,
                "score1": None,
                "score2": None,
                "winner": "",
                "url": href,
            }
        )
    return segments


def parse_match_list_from_visible_lines(raw_html: str, status_hint: str) -> List[Dict[str, Any]]:
    visible = extract_visible_text(raw_html)
    lines = visible_lines(visible)
    segments: List[Dict[str, Any]] = []
    seen = set()
    for line in lines:
        raw = collapse_ws(line)
        m = re.match(
            r"^(?P<time>(?:[A-Z][a-z]{2}\s+\d{1,2},\s*)?\d{1,2}:\d{2})\s+(?P<rest>.+?)$",
            raw,
        )
        if not m:
            continue
        rest = m.group("rest")
        status_word = ""
        if rest.lower().startswith("live "):
            status_word = "live"
            rest = rest[5:]
        elif rest.lower().startswith("full "):
            status_word = "finished"
            rest = rest[5:]
        score = re.search(r"\b(\d+)\s*-\s*(\d+)\b", rest)
        if not score:
            continue
        team1 = clean_team_text(rest[: score.start()])
        team2 = clean_team_text(rest[score.end() :])
        if not (team1 and team2):
            continue
        key = (m.group("time"), norm_key(team1), norm_key(team2), score.group(1), score.group(2))
        if key in seen:
            continue
        seen.add(key)
        s1 = int(score.group(1))
        s2 = int(score.group(2))
        winner = team1 if s1 > s2 else team2 if s2 > s1 else "draw"
        segments.append(
            {
                "raw_text": raw,
                "status": status_word or infer_status_from_text(raw, status_hint),
                "time": m.group("time"),
                "bo": "",
                "team1": team1,
                "team2": team2,
                "score1": s1,
                "score2": s2,
                "winner": winner,
                "url": "",
            }
        )
    return segments


def parse_match_list(raw_html: str, status_hint: str) -> Dict[str, Any]:
    segments = parse_match_list_from_anchors(raw_html, status_hint)
    if not segments:
        segments = parse_match_list_from_text(raw_html, status_hint)
    if not segments:
        segments = parse_match_list_from_visible_lines(raw_html, status_hint)
    return {"status": 200, "segments": segments, "count": len(segments)}


def status_from_detail_lines(lines: List[str]) -> str:
    top = "\n".join(lines[:35]).lower()
    if re.search(r"\blive\b", top):
        return "live"
    if re.search(r"\bended\b", top):
        return "finished"
    if re.search(r"\bpostponed\b", top):
        return "postponed"
    if re.search(r"\bcancelled\b", top):
        return "cancelled"
    return "unknown"


def useful_team_line(line: str) -> bool:
    if not line:
        return False
    lk = line.casefold()
    if lk in NOISE_LINES or lk in STATUS_WORDS:
        return False
    if parse_score(line):
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?[Kk%]?", line):
        return False
    if re.fullmatch(r"[wl]{1,5}", lk):
        return False
    if re.fullmatch(r"[+-]?\d+%", line):
        return False
    if len(line) > 80:
        return False
    return True


def find_line_index(lines: List[str], target: str, start: int = 0, end: Optional[int] = None) -> Optional[int]:
    if not target:
        return None
    end_i = len(lines) if end is None else min(end, len(lines))
    target_key = norm_key(target)
    for i in range(start, end_i):
        if norm_key(lines[i]) == target_key:
            return i
    return None


def parse_series_score(lines: List[str], visible: str, team1: str, team2: str) -> Dict[str, Any]:
    flat = " ".join(lines[:120])
    if team1 and team2:
        pattern = re.compile(
            re.escape(team1) + r"\s+(\d+)\s*-\s*(\d+)\s+" + re.escape(team2),
            flags=re.I | re.S,
        )
        m = pattern.search(flat)
        if m:
            s1 = int(m.group(1))
            s2 = int(m.group(2))
            winner = team1 if s1 > s2 else team2 if s2 > s1 else "draw"
            return {"score1": s1, "score2": s2, "winner": winner}

        pattern2 = re.compile(
            re.escape(team2) + r"\s+(\d+)\s*-\s*(\d+)\s+" + re.escape(team1),
            flags=re.I | re.S,
        )
        m2 = pattern2.search(flat)
        if m2:
            s2 = int(m2.group(1))
            s1 = int(m2.group(2))
            winner = team1 if s1 > s2 else team2 if s2 > s1 else "draw"
            return {"score1": s1, "score2": s2, "winner": winner}

    # BO3 detail pages commonly render top box as:
    # Ended / 1 / Team A / 0 - 2 / Team B / Full stats
    top_end = len(lines)
    for marker in ["Full stats", "Overview", "Full match Winner"]:
        idx = find_line_index(lines, marker, 0, 80)
        if idx is not None:
            top_end = min(top_end, idx)

    t1_idx = find_line_index(lines, team1, 0, top_end) if team1 else None
    t2_idx = find_line_index(lines, team2, 0, top_end) if team2 else None
    if t1_idx is not None and t2_idx is not None:
        lo = min(t1_idx, t2_idx)
        hi = max(t1_idx, t2_idx)
        for i in range(lo, hi + 1):
            score = parse_score(lines[i])
            if score:
                a, b = score
                # Assign score according to line order.
                if t1_idx < t2_idx:
                    s1, s2 = a, b
                else:
                    s2, s1 = a, b
                winner = team1 if s1 > s2 else team2 if s2 > s1 else "draw"
                return {"score1": s1, "score2": s2, "winner": winner}

    # Last fallback: first plausible score in top box, names from title/slug.
    for i, line in enumerate(lines[:top_end]):
        score = parse_score(line)
        if score:
            s1, s2 = score
            winner = team1 if team1 and s1 > s2 else team2 if team2 and s2 > s1 else "draw" if s1 == s2 else ""
            return {"score1": s1, "score2": s2, "winner": winner}

    return {"score1": None, "score2": None, "winner": ""}


def canonical_map_name(value: str) -> str:
    key = norm_key(value)
    for name in MAP_NAMES:
        if norm_key(name) == key:
            return "Dust II" if name == "Dust 2" else name
    return ""


def parse_map_scores(lines: List[str], visible: str, team1: str, team2: str) -> List[Dict[str, Any]]:
    maps: List[Dict[str, Any]] = []
    text = " ".join(lines)
    map_alt = "|".join(re.escape(x) for x in MAP_NAMES)

    # Inline form: Nuke 6 - 13
    for m in re.finditer(r"\b(" + map_alt + r")\s+(\d+)\s*-\s*(\d+)\b", text, flags=re.I):
        map_name = canonical_map_name(m.group(1)) or collapse_ws(m.group(1))
        s1 = int(m.group(2))
        s2 = int(m.group(3))
        winner = team1 if team1 and s1 > s2 else team2 if team2 and s2 > s1 else "draw"
        maps.append({"map": map_name, "score1": s1, "score2": s2, "winner": winner})

    # Split-line form:
    # Full match Winner / Nuke / 6 - 13 / Inferno / 8 - 13 / Scoreboard
    for i, line in enumerate(lines):
        map_name = canonical_map_name(line)
        if not map_name:
            continue
        # Ignore historical map winrate sections by requiring a score nearby.
        for j in range(i + 1, min(i + 5, len(lines))):
            score = parse_score(lines[j])
            if not score:
                continue
            s1, s2 = score
            winner = team1 if team1 and s1 > s2 else team2 if team2 and s2 > s1 else "draw"
            maps.append({"map": map_name, "score1": s1, "score2": s2, "winner": winner})
            break

    # Remove duplicates while preserving order.
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in maps:
        key = (item["map"].lower(), item["score1"], item["score2"])
        if key not in seen:
            deduped.append(item)
            seen.add(key)
    return deduped


def slice_between(lines: List[str], start_regex: str, stop_regex: str) -> List[str]:
    start = None
    for i, line in enumerate(lines):
        if re.search(start_regex, line, flags=re.I):
            start = i + 1
            break
    if start is None:
        return []
    end = len(lines)
    for j in range(start, len(lines)):
        if re.search(stop_regex, lines[j], flags=re.I):
            end = j
            break
    return lines[start:end]


def parse_picks_bans(lines: List[str]) -> List[Dict[str, str]]:
    chunk = slice_between(lines, r"^Picks\s*&\s*bans$", r"^(Historical|Head to head|Comments|Latest top news)")
    if not chunk:
        return []

    out: List[Dict[str, str]] = []
    i = 0
    while i < len(chunk):
        map_name = canonical_map_name(chunk[i])
        if map_name:
            action = ""
            for j in range(i + 1, min(i + 5, len(chunk))):
                if chunk[j].lower() in {"ban", "pick", "decider"}:
                    action = chunk[j].lower()
                    break
            if action:
                out.append({"map": map_name, "action": action})
        i += 1
    return out


def parse_streams(lines: List[str]) -> List[str]:
    chunk = slice_between(lines, r"^Stream$", r"^(Score predict|Team Form|Teams advantage|Lineups|Picks)")
    out: List[str] = []
    for line in chunk:
        if not re.match(r"^\d+(?:\.\d+)?[Kk]?$", line):
            out.append(line)
    return out[:10]


def parse_lineups(lines: List[str], team1: str, team2: str) -> Dict[str, List[str]]:
    chunk = slice_between(lines, r"^Lineups$", r"^Picks\s*&\s*bans$")
    if not chunk:
        return {}

    result: Dict[str, List[str]] = {}
    current_team = ""
    for line in chunk:
        if team1 and norm_key(line) == norm_key(team1):
            current_team = team1
            result.setdefault(current_team, [])
            continue
        if team2 and norm_key(line) == norm_key(team2):
            current_team = team2
            result.setdefault(current_team, [])
            continue
        if not current_team:
            continue
        lk = line.lower()
        if lk in {"lineup", "starter", "coach", "substitute"}:
            continue
        if useful_team_line(line) and line not in result[current_team]:
            result[current_team].append(line)
    return result


def parse_match_detail(raw_html: str, source_url: str) -> Dict[str, Any]:
    visible = extract_visible_text(raw_html)
    lines = visible_lines(visible)

    title = parse_title(raw_html, visible, source_url)
    team1, team2, tournament = parse_teams_from_title(title)
    if not (team1 and team2):
        team1, team2 = teams_from_match_slug(source_url)
        if team1 and team2 and not title:
            title = "%s vs %s" % (team1, team2)

    # Tournament sometimes appears right after the title if <title> did not have it.
    if not tournament:
        title_idx = find_line_index(lines, title, 0, 20)
        if title_idx is not None and title_idx + 1 < len(lines):
            cand = lines[title_idx + 1]
            if useful_team_line(cand) and " vs " not in cand:
                tournament = cand

    series = parse_series_score(lines, visible, team1, team2)
    maps = parse_map_scores(lines, visible, team1, team2)
    picks_bans = parse_picks_bans(lines)
    streams = parse_streams(lines)
    lineups = parse_lineups(lines, team1, team2)
    status = status_from_detail_lines(lines)

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
                "lineups": lineups,
            }
        ],
    }


def is_client_shell(raw_html: str) -> bool:
    if not raw_html:
        return True
    visible = extract_visible_text(raw_html)
    anchors = extract_anchors(raw_html)
    if len(visible) >= 100 or anchors:
        return False
    lower = raw_html[:20000].lower()
    return "_nuxt/" in lower and "<script" in lower


def response_mode(raw_html: str) -> str:
    if is_client_shell(raw_html):
        return "nuxt-client-shell"
    return "html"


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
    last_text = ""

    for profile_name, headers in HEADER_PROFILES:
        for attempt in range(1, 3):
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * attempt
                    await asyncio.sleep(min(delay, 10.0))
                    continue
                resp.raise_for_status()
                text = resp.text or ""
                last_text = text

                # Keep trying with bot-style profiles if BO3 only gave the Nuxt
                # empty app shell. The parser needs rendered/prerendered text.
                if is_client_shell(text) and profile_name != HEADER_PROFILES[-1][0]:
                    break

                _cache[url] = CacheEntry(time.time(), text)
                return text
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * attempt)

    if last_text:
        _cache[url] = CacheEntry(time.time(), last_text)
        return last_text
    raise HTTPException(status_code=502, detail="BO3.gg fetch failed: %s" % (last_exc,))


app = FastAPI(
    title="bo3ggapi",
    description="Unofficial REST API wrapper for public BO3.gg esports pages.",
    docs_url="/",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()


@app.get("/version", tags=["Meta"])
def version() -> Dict[str, str]:
    return {"version": "0.3.0", "default_api": "v2", "source": "bo3.gg"}


@app.get("/v2/games", tags=["Meta"])
def games() -> Dict[str, Any]:
    return {"status": "success", "data": list(CANONICAL_GAMES.values())}


@app.get("/v2/health", tags=["Meta"])
async def health(game: str = Query("cs2", description="cs2/valorant/r6s/dota2/lol/mlbb")) -> Dict[str, Any]:
    try:
        canonical = normalize_game(game)
        raw = await fetch_html(game_prefix(canonical) + "/matches/current", ttl=10)
        parsed = parse_match_list(raw, "current")
        return {
            "status": "success",
            "upstream": "ok",
            "game": canonical,
            "render_mode": response_mode(raw),
            "bytes": len(raw),
            "match_count": parsed["count"],
        }
    except Exception as exc:
        return {"status": "error", "upstream": "failed", "error": str(exc)}


@app.get("/v2/match", tags=["Matches"])
async def match(
    q: str = Query(..., description="current/live/schedule/upcoming/finished/results"),
    game: str = Query("cs2", description="cs2/valorant/r6s/dota2/lol/mlbb"),
) -> Dict[str, Any]:
    q_norm = q.lower().strip()
    try:
        canonical = normalize_game(game)
        path, hint, ttl = match_list_path(canonical, q_norm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    raw = await fetch_html(path, ttl=ttl)
    data = parse_match_list(raw, hint)
    data["game"] = canonical
    data["source_path"] = path
    data["render_mode"] = response_mode(raw)
    return {"status": "success", "data": data}


@app.get("/v2/match/all", tags=["Matches"])
async def match_all(
    q: str = Query("current", description="current/live/schedule/upcoming/finished/results"),
) -> Dict[str, Any]:
    q_norm = q.lower().strip()
    out: Dict[str, Any] = {}
    for canonical in CANONICAL_GAMES:
        try:
            path, hint, ttl = match_list_path(canonical, q_norm)
            raw = await fetch_html(path, ttl=ttl)
            parsed = parse_match_list(raw, hint)
            parsed["game"] = canonical
            parsed["source_path"] = path
            parsed["render_mode"] = response_mode(raw)
            out[canonical] = parsed
        except Exception as exc:
            out[canonical] = {"status": 502, "segments": [], "count": 0, "error": str(exc)}
    return {"status": "success", "data": out}


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

    if not is_match_detail_path(urlparse(full_url).path):
        raise HTTPException(status_code=400, detail="match details URL must be a BO3 match detail URL under /matches/")

    raw = await fetch_html(full_url, ttl=60)
    data = parse_match_detail(raw, full_url)
    data["game"] = game_from_path(urlparse(full_url).path)
    data["render_mode"] = response_mode(raw)
    return {"status": "success", "data": data}


@app.get("/v2/search", tags=["Search"])
async def search(
    q: str = Query(..., min_length=2),
    source: str = Query("finished"),
    game: str = Query("cs2", description="cs2/valorant/r6s/dota2/lol/mlbb"),
) -> Dict[str, Any]:
    source_norm = source.lower().strip()
    canonical = normalize_game(game)
    path, hint, _ttl = match_list_path(canonical, "current" if source_norm in {"current", "live", "upcoming"} else "finished")
    raw = await fetch_html(path, ttl=30)
    data = parse_match_list(raw, hint)
    q_fold = q.casefold()
    matches = []
    for item in data["segments"]:
        haystack = " ".join(
            [
                item.get("raw_text", ""),
                item.get("team1", ""),
                item.get("team2", ""),
                item.get("url", ""),
            ]
        ).casefold()
        if q_fold in haystack:
            matches.append(item)
    return {"status": "success", "data": {"status": 200, "game": canonical, "segments": matches, "count": len(matches)}}


@app.get("/v2/debug/fetch", tags=["Debug"])
async def debug_fetch(
    url: Optional[str] = Query(None, description="Full BO3.gg URL"),
    path: Optional[str] = Query(None, description="BO3.gg path"),
    game: str = Query("cs2", description="Used only when path/url is omitted"),
    q: str = Query("finished", description="current or finished; used only when path/url is omitted"),
) -> Dict[str, Any]:
    if url or path:
        target = url or path or "/matches/finished"
    else:
        try:
            target, _hint, _ttl = match_list_path(normalize_game(game), q.lower().strip())
        except ValueError:
            target = game_prefix(game) + "/matches/finished"
    full_url = normalize_url(target)
    raw = await fetch_html(full_url, ttl=0)
    visible = extract_visible_text(raw)
    anchors = extract_anchors(raw)
    match_anchors = [a for a in anchors if is_match_detail_path(canonical_match_path(urlparse(abs_bo3_url(a.get("href", ""))).path))]
    return {
        "status": "success",
        "url": full_url,
        "game": game_from_path(urlparse(full_url).path),
        "render_mode": response_mode(raw),
        "bytes": len(raw),
        "visible_chars": len(visible),
        "anchor_count": len(anchors),
        "match_anchor_count": len(match_anchors),
        "first_visible_text": visible[:DEBUG_FETCH_CHARS],
        "first_html": raw[:DEBUG_FETCH_CHARS],
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=API_PORT)
