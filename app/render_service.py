"""Page renderer.

Fetches a URL in Chromium and returns the HTML once JavaScript has run.
Called by the main service only when a plain fetch yields nothing worth
extracting, which for a list of VC sites is the Webflow and React ones.

Runs as its own Cloud Run service so the browser image is pulled only
when a site needs it.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Request
from playwright.async_api import async_playwright

INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")
NAV_TIMEOUT_MS = int(os.environ.get("RENDER_TIMEOUT_MS", "20000"))
MAX_BYTES = int(os.environ.get("RENDER_MAX_BYTES", str(3 * 1024 * 1024)))

app = FastAPI(title="Cohandle renderer")

_playwright = None
_browser = None


async def browser():
    """One browser per container, reused across requests.

    Launching Chromium costs seconds. Cloud Run keeps the container warm
    between requests within a revision, so paying that once per container
    rather than once per page is the difference between a usable crawl
    and a slow one.
    """
    global _playwright, _browser
    if _browser is None:
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
    return _browser


@app.on_event("shutdown")
async def shutdown() -> None:
    if _browser is not None:
        await _browser.close()
    if _playwright is not None:
        await _playwright.stop()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/render")
async def render(request: Request) -> dict[str, object]:
    if INTERNAL_TOKEN and request.headers.get("x-internal-token") != INTERNAL_TOKEN:
        raise HTTPException(401, "bad or missing internal token")

    body = await request.json()
    url = body.get("url")
    if not url or not str(url).startswith(("http://", "https://")):
        raise HTTPException(400, "url must be an absolute http(s) URL")

    page = await (await browser()).new_page(
        user_agent=(
            "Mozilla/5.0 (compatible; Cohandle/1.0; +https://ciit.africa) "
            "research crawler"
        )
    )
    try:
        # 'networkidle' waits for the page to settle, which is what makes
        # this worth doing over a plain fetch. It is also what makes it
        # slow, hence the timeout.
        response = await page.goto(
            url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS
        )
        html = await page.content()
        return {
            "url": page.url,
            "status": response.status if response else None,
            "html": html[:MAX_BYTES],
            "truncated": len(html) > MAX_BYTES,
        }
    except Exception as exc:  # noqa: BLE001 - surface the reason to the caller
        raise HTTPException(502, f"render failed: {type(exc).__name__}: {exc}")
    finally:
        await page.close()
