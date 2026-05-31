# bo3ggapi

Unofficial BO3.gg REST API wrapper for Vercel/FastAPI.

Supports:

- CS2
- Valorant
- Rainbow Six Siege
- Dota 2
- League of Legends
- MLBB / Mobile Legends

## Deploy to Vercel

Upload this repo to GitHub, then import it in Vercel.

Files needed:

```text
main.py
app.py
requirements.txt
vercel.json
README.md
```

## Core endpoints

```text
GET /v2/games
GET /v2/health?game=cs2
GET /v2/match?game=cs2&q=live
GET /v2/match?game=cs2&q=finished
GET /v2/match/all?q=live
GET /v2/search?game=cs2&source=finished&q=Nemesis
GET /v2/debug/fetch?game=cs2&q=finished
```

## Compact details endpoint for verifiers

Preferred usage no longer requires a BO3 URL. Use `game`, `q`, and optional team filters:

```text
GET /v2/match/details?game=cs2&q=live&team1=Team%20Nemesis&team2=FOKUS
GET /v2/match/details?game=cs2&q=finished&search=Nemesis%20FOKUS
GET /v2/match/details?game=valorant&q=results&team1=TEC%20Esports&team2=Nova%20Esports
```

Supported `q` values:

```text
current
live
schedule
upcoming
finished
results
```

Supported `game` values:

```text
cs2
valorant
r6s
dota2
lol
mlbb
```

Example response shape:

```json
{
  "status": "success",
  "data": {
    "status": 200,
    "game": "cs2",
    "query": "live",
    "source_path": "/matches/current",
    "count": 1,
    "matches": [
      {
        "match": {
          "title": "Team Nemesis vs FOKUS",
          "url": "https://bo3.gg/matches/team-nemesis-cs-vs-fokus-cs-31-05-2026",
          "status": "live",
          "bo": "bo3",
          "team1": "Nemesis",
          "team2": "FOKUS",
          "score1": 1,
          "score2": 1,
          "winner": "draw",
          "map_count": 2,
          "map_winners": [
            {"game": 1, "map": "Ancient", "winner": "FOKUS"},
            {"game": 2, "map": "Inferno", "winner": "Nemesis"}
          ]
        },
        "maps": [
          {
            "game": 1,
            "map": "Ancient",
            "team1": "Nemesis",
            "team2": "FOKUS",
            "score1": 10,
            "score2": 13,
            "winner": "FOKUS"
          }
        ]
      }
    ]
  }
}
```

For manual debugging, old URL/path mode still works:

```text
GET /v2/match/details?url=https%3A%2F%2Fbo3.gg%2Fmatches%2Fgentle-mates-cs-vs-team-nemesis-cs-31-05-2026
```

## Notes

This scrapes BO3.gg public prerendered HTML. If BO3 returns only the Nuxt client shell, check:

```text
GET /v2/debug/fetch?game=cs2&q=finished
```

Good result:

```json
{"render_mode":"html","visible_chars":100}
```

Bad result:

```json
{"render_mode":"nuxt-client-shell","visible_chars":0}
```
