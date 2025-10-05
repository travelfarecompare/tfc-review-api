import os
import re
import json
import time
import requests
from typing import List, Dict
from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup
from readability import Document
import tldextract

# Optional: load .env locally
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# -------------------------------
# Config
# -------------------------------
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_CHAT_MODEL = (os.getenv("OPENAI_CHAT_MODEL") or "gpt-4o").strip()
SERPER_API_KEY = (os.getenv("SERPER_API_KEY") or "").strip()  # optional fallback
ALLOWED_ORIGIN = (os.getenv("ALLOWED_ORIGIN") or "*").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# -------------------------------
# App bootstrap
# -------------------------------
app = Flask(_name_)
CORS(app, resources={r"/": {"origins": ALLOWED_ORIGIN}}, supports_credentials=False)

# -------------------------------
# Small helpers
# -------------------------------
def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text[:300]

def root_domain(url: str) -> str:
    ext = tldextract.extract(url)
    return ".".join([ext.domain, ext.suffix]) if ext.suffix else ext.domain

def domain_logo(url: str) -> str:
    return f"https://www.google.com/s2/favicons?sz=64&domain={root_domain(url)}"

def retry_sleep(i: int):
    # 0.5s, 1.2s, 2.0s backoff
    delays = [0.5, 1.2, 2.0]
    time.sleep(delays[min(i, len(delays)-1)])

# -------------------------------
# Content fetch for each URL
# -------------------------------
def fetch_excerpt(url: str, timeout: int = 12, attempts: int = 2) -> str:
    for i in range(attempts):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            doc = Document(r.text)
            html = doc.summary() or r.text
            soup = BeautifulSoup(html, "html.parser")

            # Prefer a non-trivial paragraph
            for p in soup.select("p"):
                line = clean_text(p.get_text())
                if len(line) > 60:
                    return line

            # fallback: meta description
            m = soup.find("meta", attrs={"name": "description"})
            if m and m.get("content"):
                return clean_text(m["content"])
        except Exception:
            retry_sleep(i)
    return ""

# -------------------------------
# Link discovery: OpenAI first
# -------------------------------
def _parse_json_strict_or_fuzzy(content: str) -> List[Dict]:
    """
    Try strict JSON first; if it fails, strip code fences and try again.
    As last resort, pull URLs via regex and create names from domains.
    Returns list of {'url','name'}.
    """
    def to_pairs(urls: List[str]) -> List[Dict]:
        out = []
        for u in urls:
            uu = u.strip()
            if not uu:
                continue
            out.append({"url": uu, "name": uu})
        return out

    # 1) strict
    try:
        parsed = json.loads(content or "{}")
        if isinstance(parsed, dict) and "links" in parsed:
            links = parsed.get("links", [])
            cleaned = []
            for it in links:
                url = (it.get("url") or "").strip()
                name = (it.get("name") or "").strip() or url
                if url:
                    cleaned.append({"url": url, "name": name})
            if cleaned:
                return cleaned
    except Exception:
        pass

    # 2) remove code fences and try again
    stripped = content.strip()
    stripped = re.sub(r"^(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*$", "", stripped)
    try:
        parsed = json.loads(stripped or "{}")
        if isinstance(parsed, dict) and "links" in parsed:
            links = parsed.get("links", [])
            cleaned = []
            for it in links:
                url = (it.get("url") or "").strip()
                name = (it.get("name") or "").strip() or url
                if url:
                    cleaned.append({"url": url, "name": name})
            if cleaned:
                return cleaned
    except Exception:
        pass

    # 3) regex URL fallback (grab up to 30)
    urls = re.findall(r"https?://[^\s\")\]}><]+", content or "")[:30]
    return to_pairs(urls)

def ask_openai_for_links(topic: str, n: int) -> List[Dict]:
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY")

    n = clamp(n, 1, 20)
    system = (
        "You are a web research assistant. Given a topic, return high-quality, "
        "editorial review links (articles or blog reviews) about that topic. "
        "Avoid homepages, category hubs, booking engines, social media, and forums. "
        "Prefer established magazines, newspapers, specialist blogs, and guides. "
        "Output strict JSON with the schema exactly: "
        '{ "links": [ { "url": "https://...", "name": "Site or Article Title" } ] }. '
        "No commentary, no markdown, no code fences."
    )
    user = f"Topic: {topic}\nReturn up to {n} review links as per schema."

    # Up to 3 attempts
    last_err = None
    for i in range(3):
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": OPENAI_CHAT_MODEL,
                    "temperature": 0.0,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    # Ask for JSON, but we'll still robustly parse
                    "response_format": {"type": "json_object"},
                },
                timeout=35,
            )
            resp.raise_for_status()
            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            links = _parse_json_strict_or_fuzzy(content)
            # Keep only http/https URLs
            links = [l for l in links if l.get("url", "").startswith(("http://", "https://"))]
            if links:
                return links[:max(n, 10)]  # return a bit more for filtering
            last_err = "OpenAI returned no usable links"
        except Exception as e:
            last_err = str(e)
        retry_sleep(i)
    raise RuntimeError(f"OpenAI error: {last_err}")

# -------------------------------
# Fallback: Serper (optional)
# -------------------------------
def serper_links(topic: str, n: int) -> List[Dict]:
    if not SERPER_API_KEY:
        return []
    try:
        headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
        payload = {"q": f"{topic} review", "num": max(10, n * 3)}
        r = requests.post("https://google.serper.dev/search", headers=headers, json=payload, timeout=25)
        r.raise_for_status()
        data = r.json()
        out = []
        for item in data.get("organic", []):
            url = (item.get("link") or "").strip()
            name = (item.get("title") or "").strip() or url
            if url:
                out.append({"url": url, "name": name})
        return out
    except Exception:
        return []

# -------------------------------
# API Endpoints
# -------------------------------
@app.route("/reviews")
def reviews():
    title = (request.args.get("title") or "").strip()
    try:
        n = int(request.args.get("n", 10))
    except Exception:
        n = 10
    n = clamp(n, 1, 20)

    if not title:
        return jsonify({"error": "Missing title", "items": []}), 400

    # 1) Ask OpenAI for links (over-ask for filtering)
    try:
        candidates = ask_openai_for_links(title, n * 2)
    except Exception as e:
        candidates = []
        openai_err = str(e)
    else:
        openai_err = ""

    # 2) If too few, try Serper fallback (optional)
    if len(candidates) < n:
        more = serper_links(title, n * 2)
        # merge unique by URL
        seen_urls = set(c["url"] for c in candidates)
        for m in more:
            if m["url"] not in seen_urls:
                candidates.append(m)
                seen_urls.add(m["url"])

    # 3) Build cards with excerpt + logo, de-dupe by root
    results = []
    seen_roots = set()
    for it in candidates:
        url = it.get("url")
        name = it.get("name") or url
        if not url:
            continue
        root = root_domain(url)
        if root in seen_roots:
            continue

        excerpt = fetch_excerpt(url)
        if not excerpt:
            continue

        results.append({
            "url": url,
            "name": name,
            "excerpt": excerpt,
            "logo": domain_logo(url),
            "score": "",
        })
        seen_roots.add(root)
        if len(results) >= n:
            break

    if not results:
        msg = "No results found."
        if openai_err:
            msg += f" ({openai_err})"
        return jsonify({"title": title, "items": [], "message": msg}), 200

    return jsonify({"title": title, "items": results}), 200

@app.route("/review-url")
def review_url():
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        doc = Document(r.text)
        html = doc.summary() or r.text
        soup = BeautifulSoup(html, "html.parser")

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

        return jsonify({
            "url": url,
            "name": name,
            "excerpt": excerpt,
            "logo": domain_logo(url),
            "score": "",
        })
    except Exception as e:
        return jsonify({"error": f"URL fetch failed: {e}"}), 500

@app.route("/health")
def health():
    return jsonify({"ok": True})

if _name_ == "_main_":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
