import os
import re
import json
import requests
from typing import List, Dict

from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup
from readability import Document
import tldextract

# Optional: load .env locally (no effect on Render unless python-dotenv is installed)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# -------------------------------
# Config
# -------------------------------
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY", "").strip())
OPENAI_CHAT_MODEL = (os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini").strip())
ALLOWED_ORIGIN = (os.getenv("ALLOWED_ORIGIN", "*").strip())

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
    """Collapse whitespace, trim, and cap to ~300 chars for our excerpts."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text[:300]

def root_domain(url: str) -> str:
    ext = tldextract.extract(url)
    return ".".join([ext.domain, ext.suffix]) if ext.suffix else ext.domain

def domain_logo(url: str) -> str:
    return f"https://www.google.com/s2/favicons?sz=64&domain={root_domain(url)}"

def fetch_excerpt(url: str, timeout: int = 10) -> str:
    """
    Pull a readable paragraph from the URL. This function is deliberately
    NON-BLOCKING: it never sleeps or raises for HTTP errors. If anything fails,
    it returns "" so the caller can skip the link quickly instead of timing out.
    """
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
    except requests.RequestException:
        return ""

    if r.status_code != 200 or not r.text:
        return ""

    try:
        # Use Readability to isolate main content; fall back to raw HTML
        doc = Document(r.text)
        html = doc.summary() or r.text
        soup = BeautifulSoup(html, "html.parser")

        # Prefer a non-trivial paragraph
        for p in soup.select("p"):
            line = clean_text(p.get_text())
            if len(line) > 60:
                return line

        # Fallbacks: meta description and og:description
        m = soup.find("meta", attrs={"name": "description"})
        if m and m.get("content"):
            return clean_text(m["content"])

        m = soup.find("meta", attrs={"property": "og:description"})
        if m and m.get("content"):
            return clean_text(m["content"])
    except Exception:
        # Any parse problem: skip
        return ""

    return ""

def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

# -------------------------------
# OpenAI call
# -------------------------------

def ask_openai_for_links(topic: str, n: int) -> List[Dict]:
    """
    Ask OpenAI to return up to n review links for the given topic.
    We instruct it to output strict JSON.
    Returns: [{"url": "...", "name": "..."}]
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY")

    n = clamp(n, 1, 20)  # keep it reasonable
    system = (
        "You are a web research assistant. Given a topic, return high-quality, "
        "editorial review links (articles or blog reviews) about that topic. "
        "Avoid homepages, category hubs, booking engines, social media, and forums. "
        "Prefer established magazines, newspapers, specialist blogs, and guides. "
        "Output strict JSON only with the schema: "
        '{ "links": [ { "url": "https://...", "name": "Site or Article Title" } ] } '
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

        # Be defensive: if the model ignored JSON format, don't crash the API
        try:
            parsed = json.loads(content or "{}")
            links = parsed.get("links", [])
        except json.JSONDecodeError:
            # Log a small breadcrumb in server logs and return empty
            print(f"[openai-json-error] content={content[:200]}")
            return []

        out: List[Dict] = []
        for it in links:
            url = (it.get("url") or "").strip()
            name = (it.get("name") or "").strip()
            if url:
                out.append({"url": url, "name": name or url})
        return out[:n]
    except Exception as e:
        # Bubble up to the route (will be JSONified nicely)
        raise RuntimeError(f"OpenAI error: {e}")

# -------------------------------
# API Endpoints
# -------------------------------

@app.route("/reviews")
def reviews():
    """
    Build review cards by topic using OpenAI to propose links, then fetch
    logo + excerpt for each link.

    Query:
      - title=Eiffel Tower
      - n=10           (optional, default 10, max 20)
    """
    title = (request.args.get("title") or "").strip()
    try:
        n = int(request.args.get("n", 10))
    except Exception:
        n = 10
    n = clamp(n, 1, 20)

    if not title:
        return jsonify({"error": "Missing title"}), 400

    try:
        # Ask for more than we need; we will filter/dedupe/skip empties
        candidates = ask_openai_for_links(title, n * 3)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    results = []
    seen_roots = set()

    for it in candidates:
        url = it.get("url")
        name = (it.get("name") or url) or ""
        if not url:
            continue

        root = root_domain(url)
        if root in seen_roots:
            continue

        excerpt = fetch_excerpt(url)
        if not excerpt:
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
        r = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"URL fetch failed: {e}"}), 500

    if r.status_code != 200 or not r.text:
        return jsonify({"error": f"URL fetch failed: HTTP {r.status_code}"}), 500

    try:
        doc = Document(r.text)
        html = doc.summary() or r.text
        soup = BeautifulSoup(html, "html.parser")

        # derive a site/article name
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

        if not excerpt:
            m = soup.find("meta", attrs={"name": "description"}) \
                or soup.find("meta", attrs={"property": "og:description"})
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
        return jsonify({"error": f"URL parse failed: {e}"}), 500

@app.route("/health")
def health():
    return jsonify({"ok": True})

# -------------------------------
# Local dev entrypoint
# -------------------------------
if __name__ == "__main__":
    # Local: python main.py
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
