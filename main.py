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
  curl "http://127.0.0.1:3002/v2/match/details?game=cs2&team1=Team%20Nemesis&team2=FOKUS"
  curl "http://127.0.0.1:3002/v2/match/details?game=cs2&search=Nemesis%20FOKUS"
  curl "http://127.0.0.1:3002/v2/match/details?game=cs2&q=finished&search=Nemesis%20FOKUS"
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
    # CS2
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
    # Valorant
    "Abyss",
    "Ascent",
    "Bind",
    "Breeze",
    "Corrode",
    "Fracture",
    "Haven",
    "Icebox",
    "Lotus",
    "Pearl",
    "Split",
    "Sunset",
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

    # BO3 sometimes mixes future/upcoming rows into the finished page.
    # For verifier use, finished/results must only return rows with a real score.
    if status_hint == "finished":
        segments = [
            item
            for item in segments
            if item.get("score1") is not None
            and item.get("score2") is not None
            and bool(item.get("winner"))
        ]

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


def is_score_line(line: str) -> bool:
    return parse_score(line) is not None


def is_int_line(line: str) -> bool:
    return bool(re.fullmatch(r"\d+", collapse_ws(line)))


def is_section_stop(line: str) -> bool:
    line = collapse_ws(line)
    return bool(
        re.search(
            r"^(Full stats|Overview|Performance|Aim|Grenades|Devices|Economy|Score predict|Stream|Analytics Insights|Team Form|Teams advantage|Lineups|Picks\s*&\s*bans|Historical|Head to head|Comments|Latest top news)",
            line,
            flags=re.I,
        )
        or re.search(r"\bScoreboard$", line, flags=re.I)
    )


def detail_top_lines(lines: List[str], max_lines: int = 120) -> List[str]:
    """Return only the match header block, excluding predictions/stats/noise."""
    out: List[str] = []
    title_seen = False
    for line in lines[:max_lines]:
        if " vs " in line:
            title_seen = True
        if title_seen and is_section_stop(line):
            break
        # Ignore global nav/header before the H1 title if present.
        if not title_seen and line.lower() in {"cs2", "valorant", "r6s", "dota 2", "lol", "mlbb", "sign in"}:
            continue
        out.append(line)
    return out


def lines_between_markers(lines: List[str], start_regex: str, stop_regex: str) -> List[str]:
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


def parse_bo_from_lines(lines: List[str]) -> str:
    for line in lines[:120]:
        m = re.search(r"\bBo([1357])\b", line, flags=re.I)
        if m:
            return "bo" + m.group(1)
    return ""


def infer_bo_from_series(score1: Optional[int], score2: Optional[int], maps: List[Dict[str, Any]]) -> str:
    if score1 is not None and score2 is not None:
        wins_needed = max(score1, score2)
        if wins_needed >= 3:
            return "bo5"
        if wins_needed == 2:
            return "bo3"
        if wins_needed == 1:
            return "bo1"
    if len(maps) >= 4:
        return "bo5"
    if len(maps) >= 2:
        return "bo3"
    if len(maps) == 1:
        return "bo1"
    return ""


def make_series(team1: str, team2: str, score1: Optional[int], score2: Optional[int]) -> Dict[str, Any]:
    winner = ""
    if score1 is not None and score2 is not None and team1 and team2:
        winner = team1 if score1 > score2 else team2 if score2 > score1 else "draw"
    return {"score1": score1, "score2": score2, "winner": winner}


def parse_series_score(lines: List[str], visible: str, team1: str, team2: str) -> Dict[str, Any]:
    """Parse only the match header score, never odds/Score predict/history."""
    top = detail_top_lines(lines)
    top_flat = " ".join(top[:80])

    if team1 and team2:
        pattern = re.compile(
            re.escape(team1) + r"\s+(\d+)\s*-\s*(\d+)\s+" + re.escape(team2),
            flags=re.I | re.S,
        )
        m = pattern.search(top_flat)
        if m:
            return make_series(team1, team2, int(m.group(1)), int(m.group(2)))

        pattern2 = re.compile(
            re.escape(team2) + r"\s+(\d+)\s*-\s*(\d+)\s+" + re.escape(team1),
            flags=re.I | re.S,
        )
        m2 = pattern2.search(top_flat)
        if m2:
            return make_series(team1, team2, int(m2.group(2)), int(m2.group(1)))

    # Common finished detail header:
    # Ended / seed-or-rank / Team A / 0 - 2 / Team B / Full stats
    t1_idx = find_line_index(top, team1, 0, len(top)) if team1 else None
    t2_idx = find_line_index(top, team2, 0, len(top)) if team2 else None
    if t1_idx is not None and t2_idx is not None:
        lo = min(t1_idx, t2_idx)
        hi = max(t1_idx, t2_idx)
        for i in range(lo, hi + 1):
            score = parse_score(top[i])
            if score:
                a, b = score
                if t1_idx < t2_idx:
                    return make_series(team1, team2, a, b)
                return make_series(team1, team2, b, a)

    # Fallback: first score-like line in the header only. This deliberately
    # excludes Score predict, odds, Team Form, H2H, and historical stats.
    for line in top[:80]:
        score = parse_score(line)
        if score:
            return make_series(team1, team2, score[0], score[1])

    return {"score1": None, "score2": None, "winner": ""}

def canonical_map_name(value: str) -> str:
    raw = collapse_ws(value)
    # BO3 labels unknown/decider maps as Map 3 / Map 5 on live pages.
    if re.fullmatch(r"(?:Map|Game)\s*\d+", raw, flags=re.I):
        return raw.title().replace("Game", "Game")

    # Some live tabs come through as "Mirage LIVE".
    raw = re.sub(r"\bLIVE\b", "", raw, flags=re.I).strip()
    key = norm_key(raw)
    for name in MAP_NAMES:
        if norm_key(name) == key:
            return "Dust II" if name == "Dust 2" else name
    return ""


def map_name_from_line(value: str) -> str:
    """Return a canonical map/game label from either a bare or noisy line."""
    raw = collapse_ws(value)
    if not raw:
        return ""
    bare = canonical_map_name(raw)
    if bare:
        return bare

    # Generic map/game labels for MOBAs and unconfirmed deciders.
    m_generic = re.search(r"\b(?:Map|Game)\s*(\d+)\b", raw, flags=re.I)
    if m_generic:
        word = "Game" if re.search(r"\bGame\b", raw, flags=re.I) else "Map"
        return "%s %s" % (word, m_generic.group(1))

    for name in MAP_NAMES:
        if re.search(r"\b" + re.escape(name) + r"\b", raw, flags=re.I):
            return "Dust II" if name == "Dust 2" else name
    return ""


def expected_maps_from_series(score1: Optional[int], score2: Optional[int], bo: str = "") -> int:
    if score1 is not None and score2 is not None:
        total = int(score1) + int(score2)
        if total > 0:
            return total
    bo_m = re.search(r"bo([1357])", bo or "", flags=re.I)
    if bo_m:
        return int(bo_m.group(1))
    return 0


def trim_maps_for_series(
    maps: List[Dict[str, Any]],
    score1: Optional[int],
    score2: Optional[int],
    bo: str = "",
) -> List[Dict[str, Any]]:
    """Keep only plausible actual map rows.

    BO3 pages repeat map names in predictions, H2H, veto, and stats sections.
    Actual match maps appear first and their count should equal the current or
    final series score total.  This makes the endpoint verifier-safe instead
    of inventing extra historical maps.
    """
    if not maps:
        return maps
    limit = expected_maps_from_series(score1, score2, bo)
    if limit and len(maps) > limit:
        return maps[:limit]
    return maps


def _score_from_int_lines(candidates: List[str]) -> Optional[Tuple[int, int]]:
    ints: List[int] = []
    for value in candidates:
        value = collapse_ws(value)
        if is_int_line(value):
            try:
                ints.append(int(value))
            except Exception:
                pass
        if len(ints) >= 2:
            return ints[0], ints[1]
    return None


def _append_map_score(
    maps: List[Dict[str, Any]],
    map_name: str,
    score1: Optional[int],
    score2: Optional[int],
    team1: str,
    team2: str,
) -> None:
    if not map_name:
        return
    winner = ""
    if score1 is not None and score2 is not None and team1 and team2:
        winner = team1 if score1 > score2 else team2 if score2 > score1 else "draw"
    maps.append(
        {
            "game": len(maps) + 1,
            "map": map_name,
            "team1": team1,
            "team2": team2,
            "score1": score1,
            "score2": score2,
            "winner": winner,
        }
    )


def _dedupe_map_scores(maps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in maps:
        map_name = item.get("map", "") or ""
        score1 = item.get("score1")
        score2 = item.get("score2")
        key = (norm_key(map_name), score1, score2)
        if key in seen:
            continue
        # Prefer scored rows. Bare map tabs without score are not useful for a
        # verifier once scored rows are available for the same map.
        if score1 is None or score2 is None:
            scored_same_map = any(
                norm_key(x.get("map", "")) == norm_key(map_name)
                and x.get("score1") is not None
                and x.get("score2") is not None
                for x in maps
            )
            if scored_same_map:
                continue
        seen.add(key)
        item = dict(item)
        item["game"] = len(deduped) + 1
        deduped.append(item)
    return deduped


def _scan_map_scores_from_lines(lines: List[str], team1: str, team2: str) -> List[Dict[str, Any]]:
    maps: List[Dict[str, Any]] = []
    stop_after_match_re = re.compile(
        r"^(?:Score predict|Stream|Analytics Insights|Team Form|Teams advantage|Lineups|"
        r"Picks\s*&\s*bans|Historical|Head to head|Comments|Latest top news)$",
        flags=re.I,
    )

    # Prefer the upper match-detail block.  It normally contains:
    #   Full stats / Ancient / 10 - 13 / Inferno / 13 - 7 / Mirage / 13 - 9
    # and appears before predictions, streams, lineups, H2H, etc.
    cutoff = len(lines)
    for i, line in enumerate(lines):
        if stop_after_match_re.search(line):
            cutoff = i
            break
    scan_lines = lines[: min(cutoff, 260)]

    for i, line in enumerate(scan_lines):
        map_name = map_name_from_line(line)
        if not map_name:
            continue

        # Avoid navigation/sport names that can accidentally contain a map word.
        if line.casefold() in {"maps", "map", "games", "game"}:
            continue

        score: Optional[Tuple[int, int]] = parse_score(line)
        if score is None:
            next_lines: List[str] = []
            for j in range(i + 1, min(i + 10, len(scan_lines))):
                if is_section_stop(scan_lines[j]) or stop_after_match_re.search(scan_lines[j]):
                    break
                if map_name_from_line(scan_lines[j]):
                    break
                next_lines.append(scan_lines[j])
                score = parse_score(scan_lines[j])
                if score is not None:
                    break
            if score is None:
                score = _score_from_int_lines(next_lines)

        if score is not None:
            _append_map_score(maps, map_name, int(score[0]), int(score[1]), team1, team2)
        else:
            # Keep unscored map tabs for live matches, but dedupe/trim later.
            _append_map_score(maps, map_name, None, None, team1, team2)

    return _dedupe_map_scores(maps)


def _scan_map_scores_from_text_blob(text: str, team1: str, team2: str) -> List[Dict[str, Any]]:
    maps: List[Dict[str, Any]] = []
    text = collapse_ws(text)
    if not text:
        return maps

    map_alt = "|".join(re.escape(x) for x in MAP_NAMES)
    generic_alt = r"(?:Map|Game)\s*\d+"
    any_map = r"(?:" + map_alt + r"|" + generic_alt + r")"

    # Visible inline form: Ancient 10 - 13
    for m in re.finditer(r"\b(?P<map>" + any_map + r")\b\s{1,80}(?P<s1>\d{1,2})\s*-\s*(?P<s2>\d{1,2})\b", text, flags=re.I):
        map_name = map_name_from_line(m.group("map"))
        _append_map_score(maps, map_name, int(m.group("s1")), int(m.group("s2")), team1, team2)

    # JSON-ish form from Nuxt payloads, when the visible text only exposes the
    # header but the page still embeds map score objects.
    json_score_keys_1 = r"(?:score1|score_1|team1Score|firstTeamScore|homeScore|scoreTeam1)"
    json_score_keys_2 = r"(?:score2|score_2|team2Score|secondTeamScore|awayScore|scoreTeam2)"
    for m in re.finditer(
        r"(?P<map>" + any_map + r")(?:(?!" + any_map + r").){0,350}?"
        r"(?:\"|'|\b)" + json_score_keys_1 + r"(?:\"|'|\b)\s*[:=]\s*(?P<s1>\d{1,2})"
        r"(?:(?!" + any_map + r").){0,180}?"
        r"(?:\"|'|\b)" + json_score_keys_2 + r"(?:\"|'|\b)\s*[:=]\s*(?P<s2>\d{1,2})",
        text,
        flags=re.I,
    ):
        map_name = map_name_from_line(m.group("map"))
        _append_map_score(maps, map_name, int(m.group("s1")), int(m.group("s2")), team1, team2)

    return _dedupe_map_scores(maps)


def parse_map_scores(lines: List[str], visible: str, raw_html: str, team1: str, team2: str) -> List[Dict[str, Any]]:
    """Parse actual played map/game rows only.

    This endpoint is meant for verification, so it should return the real match
    winner plus real per-map/game winners.  It intentionally ignores streams,
    lineups, picks/bans, odds, prediction widgets, and H2H/history sections.
    """
    maps = _scan_map_scores_from_lines(lines, team1, team2)
    if maps and any(item.get("score1") is not None and item.get("score2") is not None for item in maps):
        return maps

    # Fallback to a compact text blob.  This recovers pages where BO3 embeds
    # map scores in Nuxt/JSON-ish payloads but the visible parser only saw the
    # match header.
    blob = "\n".join([visible or "", strip_tags(raw_html or ""), html.unescape(raw_html or "")])
    maps2 = _scan_map_scores_from_text_blob(blob, team1, team2)
    if maps2:
        return maps2
    return maps

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

    status = status_from_detail_lines(lines)
    series = parse_series_score(lines, visible, team1, team2)
    bo_from_page = parse_bo_from_lines(lines)
    maps = parse_map_scores(lines, visible, raw_html, team1, team2)
    maps = trim_maps_for_series(maps, series["score1"], series["score2"], bo_from_page)
    bo = bo_from_page or infer_bo_from_series(series["score1"], series["score2"], maps)

    match_summary = {
        "title": title,
        "url": source_url,
        "status": status,
        "tournament": tournament,
        "bo": bo,
        "team1": team1,
        "team2": team2,
        "score1": series["score1"],
        "score2": series["score2"],
        "winner": series["winner"],
    }

    # Keep the old single-segment shape, but only include verifier-relevant data.
    segment = dict(match_summary)
    segment["teams"] = [
        {"name": team1, "score": series["score1"]},
        {"name": team2, "score": series["score2"]},
    ]
    segment["maps"] = maps

    return {
        "status": 200,
        "match": match_summary,
        "maps": maps,
        "segments": [segment],
    }


def merge_detail_with_list_item(data: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    """Use /matches/current or /matches/finished as a safe summary fallback.

    BO3 live detail pages can omit the opposing team's series score in the
    header, while the list row has it. This prevents Score predict odds from
    being misread as the actual winner.
    """
    if not item:
        return data

    match = data.get("match") or {}
    segments = data.get("segments") or []
    segment = segments[0] if segments else {}

    score1 = item.get("score1")
    score2 = item.get("score2")
    has_list_score = score1 is not None and score2 is not None

    if item.get("team1"):
        match["team1"] = item.get("team1")
    if item.get("team2"):
        match["team2"] = item.get("team2")
    if item.get("status"):
        match["status"] = item.get("status")
    if item.get("bo"):
        match["bo"] = item.get("bo")

    if has_list_score:
        match["score1"] = score1
        match["score2"] = score2
        match["winner"] = item.get("winner", "")
        if not match.get("bo"):
            match["bo"] = infer_bo_from_series(score1, score2, data.get("maps") or [])

    # Mirror into the old segment object.
    for key, value in match.items():
        segment[key] = value
    segment["teams"] = [
        {"name": match.get("team1", ""), "score": match.get("score1")},
        {"name": match.get("team2", ""), "score": match.get("score2")},
    ]
    segment["maps"] = data.get("maps") or []

    data["match"] = match
    data["segments"] = [segment]
    return data


async def find_match_list_item_for_detail(full_url: str, game: str) -> Dict[str, Any]:
    target_path = canonical_match_path(urlparse(full_url).path).rstrip("/")
    candidates = []
    for q_norm in ("current", "finished"):
        try:
            path, hint, ttl = match_list_path(game, q_norm)
            raw = await fetch_html(path, ttl=ttl)
            parsed = parse_match_list(raw, hint)
            candidates.extend(parsed.get("segments") or [])
        except Exception:
            continue

    for item in candidates:
        item_path = canonical_match_path(urlparse(item.get("url", "")).path).rstrip("/")
        if item_path == target_path:
            return item
    return {}


# Extra tokens to ignore when comparing BO3 team names against Polymarket names.
# This makes "Team Nemesis" match "Nemesis", "TEC Esports" match "TEC", etc.
TEAM_NAME_DROP_WORDS = {
    "team", "esport", "esports", "gaming", "gg", "cs", "cs2", "csgo",
    "valorant", "val", "lol", "dota", "dota2", "r6", "r6s", "siege",
    "mobile", "legends", "mlbb", "club", "clan", "academy",
}


def team_match_key(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", collapse_ws(value).casefold())
    kept = [w for w in words if w not in TEAM_NAME_DROP_WORDS]
    if not kept:
        kept = words
    return "".join(kept)


def team_names_close(a: str, b: str) -> bool:
    ka = team_match_key(a)
    kb = team_match_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    # Allow one side to include a harmless suffix/prefix that the other omits,
    # but avoid matching tiny tokens like "g" against "g2ares".
    if min(len(ka), len(kb)) >= 4 and (ka in kb or kb in ka):
        return True
    return False


def item_matches_team_filters(item: Dict[str, Any], team1: str = "", team2: str = "", search: str = "") -> bool:
    i1 = item.get("team1", "") or ""
    i2 = item.get("team2", "") or ""
    haystack = " ".join([
        item.get("raw_text", "") or "",
        i1,
        i2,
        item.get("url", "") or "",
    ]).casefold()

    search = collapse_ws(search or "")
    if search:
        # Search is intentionally forgiving: every non-trivial token must appear
        # somewhere in team names/raw/url after normalization.
        tokens = [
            t for t in re.findall(r"[a-z0-9]+", search.casefold())
            if len(t) >= 2 and t not in TEAM_NAME_DROP_WORDS
        ]
        normalized_haystack = norm_key(haystack)
        if tokens and not all(t in normalized_haystack for t in tokens):
            return False

    team1 = collapse_ws(team1 or "")
    team2 = collapse_ws(team2 or "")
    if team1 and team2:
        direct = team_names_close(team1, i1) and team_names_close(team2, i2)
        swapped = team_names_close(team1, i2) and team_names_close(team2, i1)
        return direct or swapped
    if team1:
        return team_names_close(team1, i1) or team_names_close(team1, i2)
    if team2:
        return team_names_close(team2, i1) or team_names_close(team2, i2)
    return True


def compact_detail_payload(data: Dict[str, Any], source_row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    match = dict(data.get("match") or {})
    maps = list(data.get("maps") or [])

    # Keep map winner labels consistent with the final match team labels.  BO3
    # live/detail pages can abbreviate team names differently than list rows.
    team1 = match.get("team1", "") or ""
    team2 = match.get("team2", "") or ""
    cleaned_maps: List[Dict[str, Any]] = []
    for idx, mp in enumerate(maps, 1):
        score1 = mp.get("score1")
        score2 = mp.get("score2")
        winner = ""
        if score1 is not None and score2 is not None and team1 and team2:
            winner = team1 if score1 > score2 else team2 if score2 > score1 else "draw"
        cleaned_maps.append({
            "game": mp.get("game") or idx,
            "map": mp.get("map", "") or "",
            "team1": team1,
            "team2": team2,
            "score1": score1,
            "score2": score2,
            "winner": winner or mp.get("winner", "") or "",
        })

    match["map_count"] = len(cleaned_maps)
    match["map_winners"] = [
        {"game": mp.get("game"), "map": mp.get("map", ""), "winner": mp.get("winner", "")}
        for mp in cleaned_maps
    ]

    out = {
        "match": match,
        "maps": cleaned_maps,
    }
    if source_row is not None:
        out["source_row"] = source_row
    return out


async def fetch_compact_detail_for_item(item: Dict[str, Any], game: str) -> Dict[str, Any]:
    full_url = item.get("url", "") or ""
    if not full_url:
        # Fallback when BO3 list rows were parsed without hrefs. No map data is
        # possible without a detail URL, but the series result is still useful.
        match_summary = {
            "title": "%s vs %s" % (item.get("team1", ""), item.get("team2", "")),
            "url": "",
            "status": item.get("status", ""),
            "tournament": "",
            "bo": item.get("bo", "") or infer_bo_from_series(item.get("score1"), item.get("score2"), []),
            "team1": item.get("team1", ""),
            "team2": item.get("team2", ""),
            "score1": item.get("score1"),
            "score2": item.get("score2"),
            "winner": item.get("winner", ""),
        }
        return compact_detail_payload({"match": match_summary, "maps": []}, item)

    raw = await fetch_html(full_url, ttl=60)
    data = parse_match_detail(raw, full_url)
    data = merge_detail_with_list_item(data, item)
    compact = compact_detail_payload(data, item)
    compact["render_mode"] = response_mode(raw)
    return compact


async def build_details_from_filters(
    q: str,
    game: str,
    team1: str = "",
    team2: str = "",
    search: str = "",
    max_results: int = 25,
) -> Dict[str, Any]:
    q_norm = (q or "both").lower().strip()
    canonical = normalize_game(game)

    # BO3.gg's live page path is /matches/current, but expose it as "live"
    # in API metadata because callers/verifiers reason about live vs finished.
    # Internally we still fetch "current" because that is the real BO3 path.
    if q_norm in {"", "all", "both", "live_finished", "live+finished", "current+finished"}:
        query_plan = ["current", "finished"]
        display_query_plan = ["live", "finished"]
        query_label = "live+finished"
    else:
        query_plan = [q_norm]
        display_query_plan = ["live" if q_norm == "current" else q_norm]
        query_label = display_query_plan[0]

    if max_results <= 0:
        max_results = 25
    max_results = max(1, min(int(max_results), 80))

    all_rows: List[Dict[str, Any]] = []
    source_paths: List[str] = []
    render_modes: List[str] = []
    query_errors: Dict[str, str] = {}

    for q_item in query_plan:
        try:
            path, hint, ttl = match_list_path(canonical, q_item)
            raw = await fetch_html(path, ttl=ttl)
            parsed = parse_match_list(raw, hint)
            source_paths.append(path)
            render_modes.append(response_mode(raw))
            for item in list(parsed.get("segments") or []):
                item = dict(item)
                item["source_query"] = "live" if q_item == "current" else q_item
                item["source_path"] = path
                all_rows.append(item)
        except Exception as exc:
            query_errors[q_item] = str(exc)

    filtered = [
        item for item in all_rows
        if item_matches_team_filters(item, team1=team1, team2=team2, search=search)
    ]

    # If exact token search failed but two teams were supplied, retry with a
    # looser combined search so "Team Nemesis" can still find BO3's "Nemesis".
    if not filtered and (team1 or team2) and not search:
        combined = " ".join([team1 or "", team2 or ""]).strip()
        if combined:
            filtered = [item for item in all_rows if item_matches_team_filters(item, search=combined)]

    # Deduplicate current/finished overlap by match URL. Prefer live/current rows
    # over finished only when the same URL appears in both lists.
    deduped_rows: List[Dict[str, Any]] = []
    seen_urls = set()
    for item in filtered:
        key = canonical_match_path(urlparse(item.get("url", "") or "").path).rstrip("/") or (
            norm_key(item.get("team1", "")), norm_key(item.get("team2", "")), str(item.get("score1")), str(item.get("score2"))
        )
        if key in seen_urls:
            continue
        seen_urls.add(key)
        deduped_rows.append(item)

    deduped_rows = deduped_rows[:max_results]

    sem = asyncio.Semaphore(6)

    async def guarded(item: Dict[str, Any]) -> Dict[str, Any]:
        async with sem:
            try:
                return await fetch_compact_detail_for_item(item, canonical)
            except Exception as exc:
                return {
                    "match": {
                        "title": "%s vs %s" % (item.get("team1", ""), item.get("team2", "")),
                        "url": item.get("url", ""),
                        "status": item.get("status", ""),
                        "bo": item.get("bo", ""),
                        "team1": item.get("team1", ""),
                        "team2": item.get("team2", ""),
                        "score1": item.get("score1"),
                        "score2": item.get("score2"),
                        "winner": item.get("winner", ""),
                        "error": str(exc),
                    },
                    "maps": [],
                    "source_row": item,
                }

    matches = await asyncio.gather(*[guarded(item) for item in deduped_rows]) if deduped_rows else []

    # Backwards/VLR-ish convenience: a flat segment per match with maps included.
    segments = []
    for item in matches:
        flat = dict(item.get("match") or {})
        flat["maps"] = item.get("maps") or []
        flat["source_row"] = item.get("source_row") or {}
        segments.append(flat)

    render_mode = "+".join(sorted(set(render_modes))) if render_modes else "unknown"
    return {
        "status": 200,
        "game": canonical,
        "query": query_label,
        "queries": display_query_plan,
        "source_path": source_paths[0] if len(source_paths) == 1 else "",
        "source_paths": source_paths,
        "render_mode": render_mode,
        "filters": {
            "team1": team1 or "",
            "team2": team2 or "",
            "search": search or "",
            "max_results": max_results,
        },
        "count": len(matches),
        "matches": matches,
        "segments": segments,
        "errors": query_errors,
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
    return {"version": "0.6.0", "default_api": "v2", "source": "bo3.gg"}


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
    q: Optional[str] = Query(None, description="Optional: live/current/schedule/upcoming/finished/results. Omit to search live+finished."),
    game: str = Query("cs2", description="cs2/valorant/r6s/dota2/lol/mlbb"),
    team1: str = Query("", description="Optional team filter, e.g. Team Nemesis"),
    team2: str = Query("", description="Optional opponent filter, e.g. FOKUS"),
    search: str = Query("", description="Optional loose text search over team names/raw row/url"),
    max_results: int = Query(25, ge=1, le=80, description="Maximum details to fetch when no URL/path is provided"),
    url: Optional[str] = Query(None, description="Optional full BO3.gg match URL; kept for backwards compatibility"),
    path: Optional[str] = Query(None, description="Optional BO3.gg match path; kept for backwards compatibility"),
) -> Dict[str, Any]:
    """Return compact match + individual map/game winners.

    Preferred verifier usage does NOT need a URL or q:
      /v2/match/details?game=cs2&team1=Team%20Nemesis&team2=FOKUS
      /v2/match/details?game=cs2&search=Nemesis%20FOKUS

    When q is omitted, this endpoint searches live/current and finished/results
    together. Response metadata reports live+finished even though BO3's live
    HTML path is /matches/current. You can still pass q=live or q=finished.

    URL/path mode is still supported for manual debugging/backwards compatibility.
    """
    target = url or path
    if target:
        try:
            full_url = normalize_url(target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if not is_match_detail_path(urlparse(full_url).path):
            raise HTTPException(status_code=400, detail="match details URL must be a BO3 match detail URL under /matches/")

        raw = await fetch_html(full_url, ttl=60)
        canonical = game_from_path(urlparse(full_url).path)
        data = parse_match_detail(raw, full_url)

        # Detail pages are best for map results. The list page is best for live
        # series score/winner when the detail page omits a side score.
        list_item = await find_match_list_item_for_detail(full_url, canonical)
        if list_item:
            data = merge_detail_with_list_item(data, list_item)

        compact = compact_detail_payload(data, list_item or None)
        compact["status"] = 200
        compact["game"] = canonical
        compact["render_mode"] = response_mode(raw)
        # Keep old VLR-style segment shape for existing callers.
        flat = dict(compact.get("match") or {})
        flat["maps"] = compact.get("maps") or []
        compact["segments"] = [flat]
        return {"status": "success", "data": compact}

    try:
        data = await build_details_from_filters(
            q=q or "both",
            game=game,
            team1=team1,
            team2=team2,
            search=search,
            max_results=max_results,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
