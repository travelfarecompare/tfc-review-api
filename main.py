import os
import re
import json
import time
from typing import List, Dict

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup
from readability import Document
import tldextract

# Optional: read a local .env during dev (safe to ignore on Render)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# -------------------------------
# Config
# -------------------------------
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_CHAT_MODEL = (os.getenv("OPENAI_CHAT_MODEL") or "gpt-4o-mini").strip()
ALLOWED_ORIGIN = (os.getenv("ALLOWED_ORIGIN") or "*").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# -------------------------------
# App bootstrap
# -------------------------------
app = Flask(__name__)
CORS(
    app,
    resources={r"/": {"origins": ALLOWED_ORIGIN if ALLOWED_ORIGIN else ""}},
    supports_credentials=False,
)

# -------------------------------
# Helpers
# -------------------------------

def clean_text(text: str) -> str:
    """Collapse whitespace, trim, and cap length to ~300 chars."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text[:300]

def root_domain(url: str) -> str:
    ext = tldextract.extract(url)
    if ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return ext.domain or url

def domain_logo(url: str) -> str:
    return f"https://www.google.com/s2/favicons?sz=64&domain={root_domain(url)}"

def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def retry_sleep(i: int):
    """Small backoff without ever exiting the process."""
    delays = [0.5, 1.0, 2.0, 3.0]
    time.sleep(delays[min(i, len(delays)-1)])

def fetch_excerpt(url: str, timeout: int = 12) -> str:
    """
    Pull a readable first-good paragraph from the URL using readability-lxml + BeautifulSoup.
    Always returns a string (empty on failure).
    """
    for i in range(3):  # a few gentle retries
        try:
            r = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            )
            # If site blocks bots, this can be 403/404/etc.
            r.raise_for_status()

            # Try readability to isolate main content
            try:
                doc = Document(r.text)
                html = doc.summary() or r.text
            except Exception:
                html = r.text

            soup = BeautifulSoup(html, "html.parser")

            # Prefer a meaningful paragraph
            for p in soup.select("p"):
                line = clean_text(p.get_text())
                if len(line) > 60:
                    return line

            # Fallback to meta description
            m = soup.find("meta", attrs={"name": "description"})
            if m and m.get("content"):
                return clean_text(m["content"])

            # Final fallback: title
            if soup.title and soup.title.string:
                return clean_text(soup.title.string)

            return ""
        except Exception:
            # network/HTTP errors: back off and retry a bit
            retry_sleep(i)
    return ""


# -------------------------------
# OpenAI call
# -------------------------------

def ask_openai_for_links(topic: str, n: int) -> List[Dict[str, str]]:
    """
    Ask OpenAI to return up to n review links for the given topic.
    Output schema requested:
      {"links":[{"url":"https://...","name":"Site or Article Title"}]}
    Returns a list of dicts with url/name (may be empty).
    """
    if not OPENAI_API_KEY:
        # No API key: return empty gracefully
        return []

    n = clamp(n, 1, 20)
    system = (
        "You are a web research assistant. Given a travel sight/topic, return high-quality "
        "editorial review links (articles or blog reviews) about that topic. "
        "Avoid homepages, category hubs, booking engines, social media, and forums. "
        "Prefer established magazines, newspapers, specialist blogs, and city/attraction guides. "
        "Respond ONLY with strict JSON using this schema: "
        '{ "links": [ { "url": "https://...", "name": "Site or Article Title" } ] }. '
        f"Return at most {n} items."
    )
    user = f"Topic: {topic}\nReturn up to {n} review links as per schema."

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_CHAT_MODEL,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )

        try:
            parsed = json.loads(content or "{}")
            links = parsed.get("links", [])
        except json.JSONDecodeError:
            # If model ever fails the JSON contract, just return empty safely
            links = []

        out: List[Dict[str, str]] = []
        for it in links:
            url = (it.get("url") or "").strip()
            name = (it.get("name") or "").strip()
            if url:
                out.append({"url": url, "name": name or url})
        return out[:n]

    except Exception:
        # Any OpenAI/network error -> return empty (never 500)
        return []


# -------------------------------
# API Endpoints
# -------------------------------

@app.route("/reviews")
def reviews():
    """
    Build review cards by topic using OpenAI to propose links, then fetch logo+excerpt.
    Query:
      - title=Eiffel Tower
      - n=10  (optional; default 10; max 20)
    """
    title = (request.args.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Missing title", "items": []}), 400

    try:
        n = int(request.args.get("n", 10))
    except Exception:
        n = 10
    n = clamp(n, 1, 20)

    # Ask for more than needed; we’ll filter & dedupe
    candidates = ask_openai_for_links(title, n * 3)

    results: List[Dict[str, str]] = []
    seen_roots = set()

    for it in candidates:
        url = (it.get("url") or "").strip()
        name = (it.get("name") or url).strip()
        if not url:
            continue

        root = root_domain(url)
        if root in seen_roots:
            continue

        excerpt = fetch_excerpt(url)
        if not excerpt:
            # Skip sites that block scraping or have thin content
            continue

        results.append(
            {
                "url": url,
                "name": name,
                "excerpt": excerpt,
                "logo": domain_logo(url),
                "score": "",
            }
        )
        seen_roots.add(root)

        if len(results) >= n:
            break

    return jsonify({"title": title, "items": results})


@app.route("/review-url")
def review_url():
    """
    Build a single review card from a direct URL.
    Query:
      - url=https://example.com/some-review
    """
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400

    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()

        try:
            doc = Document(r.text)
            html = doc.summary() or r.text
        except Exception:
            html = r.text

        soup = BeautifulSoup(html, "html.parser")

        # Name preference: title -> domain
        name = ""
        if soup.title and soup.title.string:
            name = clean_text(soup.title.string)
        if not name:
            name = root_domain(url)

        excerpt = ""
        for p in soup.select("p"):
            line = clean_text(p.get_text())
            if len(line) > 60:
                excerpt = line
                break

        # Fallbacks
        if not excerpt:
            m = soup.find("meta", attrs={"name": "description"})
            if m and m.get("content"):
                excerpt = clean_text(m["content"])

        return jsonify(
            {
                "url": url,
                "name": name,
                "excerpt": excerpt,
                "logo": domain_logo(url),
                "score": "",
            }
        )
    except Exception as e:
        # Don’t crash the API — return a clear error
        return jsonify({"error": f"URL fetch failed: {str(e)}"}), 200


@app.route("/health")
def health():
    return jsonify({"ok": True})
# -------------------------------
if __name__ == "__main__":
    # Local: python main.py
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
