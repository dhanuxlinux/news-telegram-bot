import hashlib
import html
import json
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

STATE_FILE = Path("state.json")
FEEDS_FILE = Path("feeds.txt")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MAX_ITEMS_PER_FEED = int(os.getenv("MAX_ITEMS_PER_FEED", "5"))

USER_AGENT = "Mozilla/5.0 (NewsTelegramBot/1.0)"


def load_feeds() -> list[str]:
    if not FEEDS_FILE.exists():
        raise FileNotFoundError("feeds.txt not found")
    feeds = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            feeds.append(line)
    return feeds


def load_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("sent_ids", []))
    except Exception:
        return set()


def save_state(sent_ids: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"sent_ids": sorted(sent_ids)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def first_text(elem: ET.Element, names: tuple[str, ...]) -> str:
    for child in elem.iter():
        if local_name(child.tag) in names and child.text:
            text = child.text.strip()
            if text:
                return text
    return ""


def first_link(elem: ET.Element) -> str:
    # RSS <link>text</link>
    for child in elem.iter():
        if local_name(child.tag) == "link":
            href = child.attrib.get("href")
            if href:
                return href.strip()
            if child.text and child.text.strip():
                return child.text.strip()
    return ""


def parse_date(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    raw = raw.strip()
    for parser in (parsedate_to_datetime,):
        try:
            dt = parser(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def item_id(title: str, link: str, published: str) -> str:
    base = f"{title}|{link}|{published}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def fetch_xml(url: str) -> ET.Element:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,*/*"})
    with urlopen(req, timeout=25) as resp:
        content = resp.read()
    return ET.fromstring(content)


def extract_items(root: ET.Element) -> list[dict]:
    items = []

    # RSS 2.0
    channel = root.find("channel")
    if channel is not None:
        for node in channel.findall("item"):
            title = first_text(node, ("title",))
            link = first_link(node)
            desc = first_text(node, ("description", "encoded"))
            pub = first_text(node, ("pubDate", "date"))
            guid = first_text(node, ("guid",))
            items.append(
                {
                    "title": title,
                    "link": link,
                    "description": desc,
                    "published": pub,
                    "id": guid or item_id(title, link, pub),
                }
            )
        return items

    # Atom
    for node in root.findall("{http://www.w3.org/2005/Atom}entry"):
        title = first_text(node, ("title",))
        link = ""
        for child in node:
            if local_name(child.tag) == "link":
                href = child.attrib.get("href")
                if href:
                    link = href.strip()
                    break
        desc = first_text(node, ("summary", "content"))
        pub = first_text(node, ("published", "updated"))
        feed_id = first_text(node, ("id",))
        items.append(
            {
                "title": title,
                "link": link,
                "description": desc,
                "published": pub,
                "id": feed_id or item_id(title, link, pub),
            }
        )
    return items


def send_telegram(text: str) -> None:
    import urllib.parse
    import urllib.request

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=25) as resp:
        resp.read()


def build_message(feed_url: str, item: dict) -> str:
    title = html.escape(item["title"] or "No title")
    link = html.escape(item["link"] or feed_url)
    desc = html.escape((item["description"] or "").strip())

    parts = [
        f"<b>{title}</b>",
        f'<a href="{link}">Open article</a>',
    ]
    if desc:
        parts.append(desc[:700])
    return "\n".join(parts)


def main() -> None:
    feeds = load_feeds()
    sent_ids = load_state()
    new_count = 0

    for feed_url in feeds:
        try:
            root = fetch_xml(feed_url)
            items = extract_items(root)
        except (HTTPError, URLError, ET.ParseError, TimeoutError, Exception) as e:
            print(f"[ERROR] {feed_url}: {e}")
            continue

        # newest first if dates exist
        def sort_key(x):
            dt = parse_date(x.get("published", ""))
            return dt or datetime.min.replace(tzinfo=timezone.utc)

        items = sorted(items, key=sort_key, reverse=True)[:MAX_ITEMS_PER_FEED]

        for item in items:
            if item["id"] in sent_ids:
                continue

            message = build_message(feed_url, item)
            try:
                send_telegram(message)
                sent_ids.add(item["id"])
                new_count += 1
                print(f"[OK] Sent: {item['title']}")
            except Exception as e:
                print(f"[ERROR] Telegram send failed: {e}")

    save_state(sent_ids)
    print(f"Done. New items sent: {new_count}")


if __name__ == "__main__":
    main()
