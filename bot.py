from __future__ import annotations

import os
import re
import time
import socket
import sqlite3
import urllib.parse
import requests
import urllib3.util.connection as urllib3_cn
import feedparser
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from googlenewsdecoder import gnewsdecoder

# Force IPv4 so Indian ISPs don't hang on IPv6 routes to api.telegram.org
def allowed_gai_family():
    return socket.AF_INET

urllib3_cn.allowed_gai_family = allowed_gai_family

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
AMAZON_TAG = os.getenv("AMAZON_TAG", "").strip()
CUELINKS_API_KEY = os.getenv("CUELINKS_API_KEY", "").strip()

# Direct site:amazon.in queries return real Indian product ASINs immediately
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=site:amazon.in+deal+OR+price+drop+when:2d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=site:amazon.in/dp/+discount+OR+offer+when:3d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://www.grabon.in/indulge/feed/",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect("posted_deals.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posted_deals (
            deal_id TEXT PRIMARY KEY,
            title TEXT,
            posted_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def is_already_posted(conn: sqlite3.Connection, deal_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM posted_deals WHERE deal_id = ?", (deal_id,))
    return cur.fetchone() is not None


def mark_as_posted(conn: sqlite3.Connection, deal_id: str, title: str):
    conn.execute(
        "INSERT OR IGNORE INTO posted_deals (deal_id, title) VALUES (?, ?)",
        (deal_id, title),
    )
    conn.commit()


def clean_amazon_url(url: str) -> str | None:
    decoded = urllib.parse.unquote(url)
    match = re.search(r"(/dp/|/gp/product/|/gp/aw/d/)([A-Z0-9]{10})", decoded)
    if match:
        return f"https://www.amazon.in/dp/{match.group(2)}"
    return None


def resolve_article_url(url: str) -> str:
    if "news.google.com" in url:
        try:
            result = gnewsdecoder(url, interval=1)
            if result.get("status") and result.get("decoded_url"):
                return result["decoded_url"]
        except Exception as e:
            print(f"[WARN] Google News decode error: {e}")
    return url


def find_amazon_product_in_page(raw_link: str) -> tuple[str | None, str | None]:
    article_url = resolve_article_url(raw_link)

    if "amazon.in" in article_url:
        clean = clean_amazon_url(article_url)
        if clean:
            return clean, None

    try:
        resp = requests.get(article_url, headers=HEADERS, allow_redirects=True, timeout=15)
        if "amazon.in" in resp.url:
            clean = clean_amazon_url(resp.url)
            if clean:
                return clean, None

        soup = BeautifulSoup(resp.text, "html.parser")

        image_url = None
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content", "").startswith("http"):
            image_url = og_img["content"]

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "amazon.in" in href:
                clean = clean_amazon_url(href)
                if clean:
                    return clean, image_url
            if "amzn.to" in href or "amzn.in" in href:
                try:
                    r2 = requests.get(href, headers=HEADERS, allow_redirects=True, timeout=10, stream=True)
                    resolved = r2.url
                    r2.close()
                    clean = clean_amazon_url(resolved)
                    if clean:
                        return clean, image_url
                except Exception:
                    pass
    except Exception as e:
        print(f"[WARN] Article fetch failed ({article_url[:50]}): {e}")

    return None, None


def escape_markdown(text: str) -> str:
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def clean_headline(raw_title: str) -> str:
    return re.sub(r"\s+-\s+[^-]+$", "", raw_title).strip()


def send_telegram_with_retry(endpoint: str, payload: dict, retries: int = 3) -> requests.Response | None:
    """Retries Telegram API calls with generous timeout to handle slow ISP routing."""
    for attempt in range(1, retries + 1):
        try:
            return requests.post(endpoint, json=payload, timeout=30)
        except requests.exceptions.RequestException as e:
            print(f"[WARN] Telegram network timeout (attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(3)
    return None


def post_to_telegram(title: str, store_url: str, image_url: str | None) -> bool:
    aff_url = f"{store_url}?tag={AMAZON_TAG}" if AMAZON_TAG else store_url
    safe_title = escape_markdown(clean_headline(title)[:240])

    caption = (
        f"🔥 *{safe_title}*\n\n"
        f"🏪 *Store:* `amazon.in`\n"
        f"⚡ _Verified deal alert—grab before price changes!_"
    )[:1000]

    reply_markup = {
        "inline_keyboard": [[{"text": "🛒 Grab Deal Now", "url": aff_url}]]
    }

    if image_url:
        endpoint = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": CHANNEL_ID,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "Markdown",
            "reply_markup": reply_markup,
        }
    else:
        endpoint = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHANNEL_ID,
            "text": caption,
            "parse_mode": "Markdown",
            "reply_markup": reply_markup,
        }

    resp = send_telegram_with_retry(endpoint, payload)
    if resp is not None and not resp.ok and image_url:
        resp = send_telegram_with_retry(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            {
                "chat_id": CHANNEL_ID,
                "text": caption,
                "parse_mode": "Markdown",
                "reply_markup": reply_markup,
            },
        )

    if resp is None or not resp.ok:
        print(f"[ERROR] Telegram API failed: {resp.text if resp is not None else 'No response'}")
        return False
    return True


def run():
    if not BOT_TOKEN or not CHANNEL_ID:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL_ID in .env")

    conn = init_db()
    posted_count = 0

    for feed_url in RSS_FEEDS:
        if posted_count >= 3:
            break

        print(f"[INFO] Fetching: {feed_url}")
        resp = requests.get(feed_url, headers=HEADERS, timeout=15)
        feed = feedparser.parse(resp.text)
        print(f"[DEBUG] Entries found: {len(feed.entries)}")

        for entry in feed.entries[:25]:
            if posted_count >= 3:
                break

            title = entry.get("title", "").strip()
            raw_link = entry.get("link", "")

            if not raw_link or is_already_posted(conn, raw_link):
                continue

            store_url, image_url = find_amazon_product_in_page(raw_link)
            if not store_url:
                continue

            if is_already_posted(conn, store_url):
                continue

            print(f"[INFO] Posting: {clean_headline(title)[:55]} -> {store_url}")
            if post_to_telegram(title, store_url, image_url):
                mark_as_posted(conn, raw_link, title)
                mark_as_posted(conn, store_url, title)
                posted_count += 1
                time.sleep(3)

    print(f"[DONE] Run complete. Posted {posted_count} new deal(s).")


if __name__ == "__main__":
    run()