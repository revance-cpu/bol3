# bo3ggapi

Unofficial BO3.gg REST API wrapper for CS2 public pages. Python/FastAPI, deployable to Vercel.

## Endpoints

```text
GET /v2/match?q=current
GET /v2/match?q=live
GET /v2/match?q=upcoming
GET /v2/match?q=finished
GET /v2/match?q=results
GET /v2/match/details?url=<bo3 match url>
GET /v2/search?q=<team>&source=finished
GET /v2/health
GET /v2/debug/fetch?path=/matches/finished
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

## Notes

This scrapes BO3.gg public HTML. Use `/v2/match/details` as the source of truth for verification because detail pages expose cleaner series score and map scores than the list rows.

If Vercel returns empty results, try:

```text
/v2/debug/fetch?path=/matches/finished
```

That endpoint shows whether BO3.gg returned normal HTML, blocked HTML, or a changed page shape.
