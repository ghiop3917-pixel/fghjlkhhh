import hashlib
import ipaddress
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from studio.db import Store


def public_https(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Разрешён только публичный HTTPS без учётных данных")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Внутренние адреса источников запрещены")


async def collect(store: Store, urls: list[str]):
    found = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
        for url in urls[:12]:
            try:
                public_https(url)
                async with client.stream("GET", url, headers={"User-Agent": "CartoonStudio/0.1 RSS"}) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for part in response.aiter_bytes():
                        content.extend(part)
                        if len(content) > 2_000_000:
                            raise ValueError("RSS превышает лимит размера")
                if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                    raise ValueError("DTD запрещён")
                root = ET.fromstring(content)
                items = root.findall(".//item") + root.findall("{http://www.w3.org/2005/Atom}entry")
                for item in items[:40]:
                    def value(tag):
                        node = item.find(tag)
                        if node is None:
                            node = item.find("{http://www.w3.org/2005/Atom}" + tag)
                        return (node.text or "").strip() if node is not None else ""
                    title = value("title")[:250]
                    date = value("pubDate") or value("published") or value("updated")
                    try:
                        stamp = parsedate_to_datetime(date) if "," in date else datetime.fromisoformat(date.replace("Z", "+00:00"))
                        if stamp.tzinfo is None:
                            continue
                        age = (datetime.now(timezone.utc) - stamp).total_seconds()
                        if age < -300 or age > 72 * 3600:
                            continue
                    except (ValueError, TypeError, OverflowError):
                        continue
                    link = value("link")
                    if not link:
                        node = item.find("{http://www.w3.org/2005/Atom}link")
                        link = node.get("href", "") if node is not None else ""
                    if not title or urlparse(link).scheme != "https":
                        continue
                    id = hashlib.sha256(title.casefold().encode()).hexdigest()[:20]
                    if store.get("trend", id):
                        continue
                    topic = {"title": title, "url": link, "source": url, "published": stamp.isoformat(), "observed": datetime.now(timezone.utc).isoformat(),
                             "verification": "single_source", "popularity": None, "competition": None,
                             "editorial_score": round(max(0, 1-age/(72*3600)), 3), "status": "proposed"}
                    store.put("trend", id, topic)
                    found.append({"id": id, **topic})
            except (httpx.HTTPError, ValueError, ET.ParseError, OSError):
                store.event("trend.source_failed", {"host": urlparse(url).hostname})
    return sorted(found, key=lambda t: t["editorial_score"], reverse=True)[:5]
