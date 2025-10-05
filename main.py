import os
import re
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup
from readability import Document
import tldextract

app = Flask(_name_)
CORS(app)

# Prefer env var; fall back to your provided key
SERPER_API_KEY = os.getenv(
    "SERPER_API_KEY",
    "bf878008033cf77401339961b95a21f9dc3567b4"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0 Safari/537.36"
)

def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:300]

def domain_logo(url: str) -> str:
    dom = tldextract.extract(url)
    domain = ".".join([dom.domain, dom.suffix])
    return f"https://www.google.com/s2/favicons?sz=64&domain={domain}"

def fetch_excerpt(url: str) -> str:
    """Pull a readable first good paragraph from the URL."""
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        doc = Document(r.text)
        html = doc.summary()
        soup = BeautifulSoup(html, "html.parser")
        for p in soup.select("p"):
            line = clean_text(p.get_text())
            if len(line) > 60:
                return line
    except Exception:
        pass
    return ""

@app.route("/reviews")
def reviews():
    """
    Search by title and return up to n review items.
    Query: ?title=Eiffel+Tower&n=10
    """
    title = request.args.get("title", "").strip()
    try:
        n = int(request.args.get("n", 10))  # default 10
    except Exception:
        n = 10

    if not title:
        return jsonify({"error": "Missing title"}), 400

    if not SERPER_API_KEY:
        return jsonify({"error": "Missing SERPER_API_KEY"}), 500

    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    # Ask Serper for more than we need, then filter/dedupe and cap to n
    payload = {"q": f"{title} review", "num": max(10, n * 3)}

    try:
        res = requests.post(
            "https://google.serper.dev/search",
            headers=headers,
            json=payload,
            timeout=25,
        )
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        return jsonify({"error": f"Serper fetch failed: {str(e)}"}), 500

    results = []
    seen_roots = set()

    for item in data.get("organic", []):
        url = item.get("link")
        name = item.get("title") or url
        if not url or not name:
            continue

        root = ".".join([tldextract.extract(url).domain, tldextract.extract(url).suffix])
        if root in seen_roots:
            continue
        seen_roots.add(root)

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

        if len(results) >= n:
            break

    return jsonify({"title": title, "items": results})

@app.route("/review-url")
def review_url():
    """
    Fetch one review by direct URL.
    Query: ?url=https://example.com/review
    """
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400

    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        doc = Document(r.text)
        html = doc.summary()
        soup = BeautifulSoup(html, "html.parser")

        # Try to derive a nice site/page name
        name = ""
        if soup.title and soup.title.string:
            name = clean_text(soup.title.string)
        if not name:
            ext = tldextract.extract(url)
            name = ".".join([ext.domain, ext.suffix])

        excerpt = ""
        for p in soup.select("p"):
            line = clean_text(p.get_text())
            if len(line) > 60:
                excerpt = line
                break

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
        return jsonify({"error": f"URL fetch failed: {str(e)}"}), 500

@app.route("/health")
def health():
    return jsonify({'ok': True})
