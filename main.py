import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup
from readability import Document
import tldextract
import re

app = Flask(_name_)
CORS(app)

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
MAX_RESULTS = 12
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"

def clean_text(text):
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300]

def domain_logo(url):
    dom = tldextract.extract(url)
    domain = ".".join([dom.domain, dom.suffix])
    return f"https://www.google.com/s2/favicons?sz=64&domain={domain}"

def fetch_excerpt(url):
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
        return ""
    return ""

@app.route("/reviews")
def reviews():
    title = request.args.get("title", "").strip()
    n = int(request.args.get("n", 6))

    if not title:
        return jsonify({"error": "Missing title"}), 400

    if not SERPER_API_KEY:
        return jsonify({"error": "Missing SERPER_API_KEY"}), 500

    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "q": f"{title} review",
        "num": max(10, n * 3)
    }

    try:
        res = requests.post("https://google.serper.dev/search", headers=headers, json=payload, timeout=25)
        res.raise_for_status()
        data = res.json()
        results = []

        seen = set()
        for item in data.get("organic", []):
            url = item.get("link")
            name = item.get("title") or url
            if not url or not name:
                continue

            dom = tldextract.extract(url)
            root = ".".join([dom.domain, dom.suffix])
            if root in seen:
                continue
            seen.add(root)

            excerpt = fetch_excerpt(url)
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

        return jsonify({"title": title, "items": results})
    except Exception as e:
        return jsonify({"error": f"Fetch failed: {str(e)}"}), 500
