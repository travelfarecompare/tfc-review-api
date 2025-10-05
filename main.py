import os
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict
from bs4 import BeautifulSoup
from readability import Document
import tldextract

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")

app = FastAPI(title="Google Reviews API (via Serper.dev)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN] if ALLOWED_ORIGIN != "" else [""],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def clean_text(text: str) -> str:
    import re
    txt = re.sub(r"\s+", " ", text).strip()
    return txt[:300]

def domain_logo(url: str) -> str:
    dom = tldextract.extract(url)
    domain = ".".join([dom.domain, dom.suffix])
    return f"https://www.google.com/s2/favicons?sz=64&domain={domain}"

async def fetch_excerpt(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, timeout=12)
        r.raise_for_status()
        doc = Document(r.text)
        html = doc.summary()
        soup = BeautifulSoup(html, "html.parser")
        for p in soup.select("p"):
            line = clean_text(p.get_text(strip=True))
            if len(line) > 60:
                return line
    except Exception:
        pass
    return ""

@app.get("/reviews")
async def get_reviews(title: str, n: int = 10):
    if not title:
        raise HTTPException(status_code=400, detail="Missing title")

    if not SERPER_API_KEY:
        raise HTTPException(status_code=500, detail="Missing SERPER_API_KEY")

    headers = {"X-API-KEY": SERPER_API_KEY}
    payload = {"q": f"{title} review", "num": max(10, n * 3)}
    results = []

    async with httpx.AsyncClient() as client:
        try:
            r = await client.post("https://google.serper.dev/search", headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            organic = data.get("organic", [])
            seen = set()

            for item in organic:
                url = item.get("link")
                name = item.get("title") or url
                if not url or not name:
                    continue
                root = tldextract.extract(url)
                root = ".".join([root.domain, root.suffix])
                if root in seen:
                    continue
                seen.add(root)
                excerpt = await fetch_excerpt(client, url)
                if excerpt:
                    results.append({
                        "url": url,
                        "name": name,
                        "excerpt": excerpt,
                        "logo": domain_logo(url),
                        "score": ""
                    })
                if len(results) >= n:
                    break

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Serper fetch failed: {str(e)}")

    return {"title": title, "items": results}
