# main.py
import os, re, json, asyncio
import httpx
from typing import List, Dict
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
from readability import Document
import tldextract

SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

def clean_text(txt: str) -> str:
    txt = re.sub(r"\s+", " ", (txt or "")).strip()
    return txt[:300]

def root_domain(url: str) -> str:
    ext = tldextract.extract(url)
    return ".".join(p for p in [ext.domain, ext.suffix] if p)

def domain_logo(url_or_domain: str) -> str:
    d = url_or_domain
    if d.startswith("http"):
        d = root_domain(d)
    return f"https://www.google.com/s2/favicons?sz=64&domain={d}"

def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=10.0)
    )

async def fetch_excerpt(client: httpx.AsyncClient, url: str) -> str:
    # Try page → readability → first non-trivial paragraph → fallback meta description
    try:
        r = await client.get(url)
        r.raise_for_status()
        html_raw = r.text
        try:
            doc = Document(html_raw)
            html = doc.summary() or html_raw
        except Exception:
            html = html_raw
        soup = BeautifulSoup(html, "html.parser")

        for p in soup.select("p"):
            line = clean_text(p.get_text(" ", strip=True))
            if len(line) > 60:
                return line

        m = soup.find("meta", attrs={"name": "description"})
        if m and m.get("content"):
            return clean_text(m["content"])

    except Exception:
        pass
    return ""

app = FastAPI(title="TFC Reviews API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[""] if ALLOWED_ORIGIN == "" else [ALLOWED_ORIGIN],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/reviews")
async def reviews(title: str = Query(..., min_length=2), n: int = Query(6, ge=1, le=20)):
    if not SERPER_API_KEY:
        raise HTTPException(status_code=500, detail="Missing SERPER_API_KEY")

    payload = {"q": f"{title} review", "num": max(10, n * 3)}
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}

    results: List[Dict] = []
    seen = set()

    async with make_client() as client:
        try:
            resp = await client.post("https://google.serper.dev/search", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Serper fetch failed: {e}")

        for item in data.get("organic", []):
            url = (item.get("link") or "").strip()
            name = (item.get("title") or url).strip()
            if not url:
                continue

            r = root_domain(url)
            if not r or r in seen:
                continue

            excerpt = await fetch_excerpt(client, url)
            if not excerpt:
                continue

            results.append({
                "url": url,
                "name": name,
                "excerpt": excerpt,
                "logo": domain_logo(url),
                "score": ""
            })
            seen.add(r)
            if len(results) >= n:
                break

    return {"title": title, "items": results}

@app.get("/review-url")
async def review_url(url: str = Query(..., min_length=8)):
    # Build one review card from a direct URL
    async with make_client() as client:
        try:
            r = await client.get(url)
            r.raise_for_status()
            html_raw = r.text
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"URL fetch failed: {e}")

        try:
            doc = Document(html_raw)
            html = doc.summary() or html_raw
        except Exception:
            html = html_raw

        soup = BeautifulSoup(html, "html.parser")

        # site/article name
        name = ""
        if soup.title and soup.title.string:
            name = clean_text(soup.title.string)
        if not name:
            name = root_domain(url) or url

        # excerpt
        excerpt = ""
        for p in soup.select("p"):
            line = clean_text(p.get_text(" ", strip=True))
            if len(line) > 60:
                excerpt = line
                break
        if not excerpt:
            m = soup.find("meta", attrs={"name": "description"})
            if m and m.get("content"):
                excerpt = clean_text(m["content"])

        return {
            "url": url,
            "name": name,
            "excerpt": excerpt,
            "logo": domain_logo(url),
            "score": ""
        }
