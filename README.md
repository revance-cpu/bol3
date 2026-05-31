# bo3ggapi — Vercel-ready starter

Unofficial REST wrapper for public BO3.gg CS2 pages, modeled after a VLR API-style wrapper.

## Local run

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

Open:

```text
http://127.0.0.1:3002/
```

## Deploy to Vercel through GitHub

1. Create a new GitHub repo.
2. Upload these files to the repo root:
   - `main.py`
   - `app.py`
   - `requirements.txt`
   - `vercel.json`
   - `README.md`
3. In Vercel, choose **Add New → Project**.
4. Import the GitHub repo.
5. Leave framework/build settings as auto/default.
6. Deploy.

Vercel entrypoint is `app.py`, which imports the FastAPI `app` from `main.py`.

## Endpoints

```bash
curl "https://YOUR-VERCEL-DOMAIN.vercel.app/v2/match?q=current"
curl "https://YOUR-VERCEL-DOMAIN.vercel.app/v2/match?q=finished"
curl "https://YOUR-VERCEL-DOMAIN.vercel.app/v2/search?q=G2&source=finished"
curl "https://YOUR-VERCEL-DOMAIN.vercel.app/v2/match/details?url=https://bo3.gg/matches/atreides-vs-g2-ares-31-05-2026"
```

## Notes

- This uses BO3.gg public HTML. It does not use Playwright.
- List rows are best-effort because some BO3 list cards compress multi-word team names.
- The detail endpoint is more reliable for winner/map-score verification.
- The code is Python 3.9-compatible, but Vercel currently runs Python functions on its supported Python versions.
