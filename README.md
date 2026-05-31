# bo3ggapi

Unofficial BO3.gg REST API wrapper for public esports match pages. Python/FastAPI, deployable to Vercel.

## Supported BO3 game namespaces

BO3.gg does **not** search every game from `/matches/*`.

| game param | BO3 path |
|---|---|
| `cs2` | `/matches/current`, `/matches/finished` |
| `valorant` | `/valorant/matches/current`, `/valorant/matches/finished` |
| `r6s` | `/r6siege/matches/current`, `/r6siege/matches/finished` |
| `dota2` | `/dota2/matches/current`, `/dota2/matches/finished` |
| `lol` | `/lol/matches/current`, `/lol/matches/finished` |
| `mlbb` | `/mlbb/matches/current`, `/mlbb/matches/finished` |

## Endpoints

```text
GET /v2/games
GET /v2/match?game=cs2&q=current
GET /v2/match?game=cs2&q=finished
GET /v2/match?game=valorant&q=finished
GET /v2/match?game=r6s&q=finished
GET /v2/match?game=dota2&q=finished
GET /v2/match?game=lol&q=finished
GET /v2/match?game=mlbb&q=finished
GET /v2/match/all?q=current
GET /v2/match/all?q=finished
GET /v2/match/details?url=<bo3 match url>
GET /v2/search?game=cs2&q=<team>&source=finished
GET /v2/health?game=cs2
GET /v2/debug/fetch?game=cs2&q=finished
GET /version
```

## Deploy to Vercel

Upload these files to GitHub, then import the repo in Vercel:

```text
main.py
app.py
requirements.txt
vercel.json
README.md
```

## Local run

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

Open:

```text
http://127.0.0.1:3002/
```

## Important Vercel note

BO3.gg is Nuxt-based. Normal server-side HTTP clients may receive only an empty Nuxt client shell with 0 visible chars and 0 anchors. This build tries desktop, Googlebot, Bingbot, and Facebook crawler-style headers before returning. If `/v2/debug/fetch` still says `render_mode: nuxt-client-shell`, BO3 did not return prerendered HTML to Vercel and the next step is to use BO3's internal JSON endpoint or a browser-rendered source.
