## Purpose

Quick, repo-specific instructions for AI coding agents working on this project.
Focus: how the app is structured, entrypoints, key parsing patterns, and concrete examples to edit or extend.

## Quick facts
- Language: Python 3.12
- Framework: FastAPI application in `main.py` (single-file service)
- Run method: `uvicorn main:app` (Dockerfile uses this as the CMD)
- Docker image: `python:3.12-slim` with pinned libs in the Dockerfile

## Key files
- `main.py` — single FastAPI app. Core endpoints: `GET /scrape` and `GET /healthz`.
- `Dockerfile` — shows how the image is built and the exact runtime command used (`uvicorn main:app --host 0.0.0.0 --port 8080`).

## Big-picture architecture (discoverable from files)
- This is a small HTTP microservice that scrapes public Telegram channel web pages (t.me).
- Flow: request to `/scrape?username=...` -> `_fetch` pulls HTML (uses `httpx` async client) -> BeautifulSoup parses HTML -> `_parse_channel_meta` + `_collect_posts` return structured JSON.
- Pagination: `_collect_posts` iterates pages by reading message ids from `a.tgme_widget_message_date` anchors and uses `?before=` to page. Max posts = 100, pages capped at 20 (safety cap).

## Important patterns and conventions (use these when editing)
- Selector-based scraping: code relies on CSS selectors like `.tgme_widget_message_wrap`, `.tgme_widget_message_text`, `.tgme_widget_message_views`, `.tgme_channel_info_counters .tgme_channel_info_counter` — change selectors only after verifying in a live HTML sample.
- Dedup and pagination: dedup keys are (timestamp, first 50 chars of text). Keep similar logic when adding other de-dupe heuristics.
- Numeric parsing: `_parse_int_from_text` handles `1,234`, `12.3K`, `2.5M` and odd whitespace (NBSP). Use it for any count parsing.
- Retry semantics: `_fetch` is wrapped with `tenacity` (3 attempts, exponential backoff). HTTP 404 -> TelegramHTTPError which is used to drive retry/404 logic. Preserve this pattern for transient errors.

## How to run locally (dev) — (PowerShell examples)
- Run with autoreload:
```powershell
pip install -r requirements.txt  # not present by default; Dockerfile pins deps. Otherwise install pip packages shown in Dockerfile
uvicorn main:app --reload --host 0.0.0.0 --port 8080
```
- Quick request example:
```powershell
curl "http://localhost:8080/scrape?username=example_channel"
```

## How to build/run with Docker (Cloud Run friendly)
- Build and run locally (PowerShell):
```powershell
docker build -t tg-scraper:local .
docker run -p 8080:8080 tg-scraper:local
```
- Note: Dockerfile sets `PORT=8080` in env and uses `uvicorn main:app --port 8080` so Cloud Run / other platforms that expect `$PORT` should work without edits.

## API contract & examples
- `GET /scrape?username=<username>`
  - Query validation: username must match the pattern in `main.py` (alphanumeric, underscore, dot, 2-64 chars). See the `Query(..., pattern=...)` decorator.
  - Response model: `ScrapeResult` (channel meta + list of `Post` objects). See `Post` and `ScrapeResult` Pydantic models in `main.py`.

## Integration points / external dependencies
- Calls live to `https://t.me/s/<username>` (the public Telegram channel web view). Network access required for runtime.
- Uses `httpx` async client, `BeautifulSoup` with `lxml` parser, `tenacity` for retries.

## When making changes, prefer small, verifiable edits
- If changing parsing selectors, include a unit test that loads a saved sample HTML snip to avoid regressions.
- If adding dependencies, reflect them in Dockerfile or add a `requirements.txt` so local dev and CI match Docker build.

## Known limits (documented in code)
- `max_posts` is capped to 100 in the public API; `_collect_posts` has a pages cap of 20. Any change to limits should update the method and any consumer expectations.

## Examples to reference when coding
- Entrypoint/CMD in `Dockerfile`:
  `CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]`
- Main endpoint decorator in `main.py`:
  `@app.get("/scrape", response_model=ScrapeResult)`

## If something's unclear
- Ask for a representative sample HTML page from Telegram for the channel(s) you intend to target when changing selectors.
- If you want CI, tests, or `requirements.txt` added, request it and I'll scaffold those files.

---
If you'd like, I can add a small test harness and `requirements.txt` so local dev matches Docker behavior. Any specific areas you want expanded? 
