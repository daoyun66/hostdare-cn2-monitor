#!/usr/bin/env python3
"""Daily IPv6-only VPS deal finder. Strict, non-purchasing, best-effort discovery."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

MAX_USD = 20.0
MIN_RAM_MB = 1024
STATE = Path("ipv6_state.json")
REPORT = Path("ipv6_last_check.json")
TIMEOUT = 18
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; IPv6VPSDealMonitor/1.0)",
           "Accept-Language": "en-US,en;q=0.9"}

# Official merchant pages only. Extend this list as vendors are validated.
SOURCES = {
    "CloudCone": ["https://cloudcone.com/"],
    "SiteHUB": ["https://sitehub.agency/v6-only/"],
    "SmokyHosts": ["https://smokyhosts.com/ipv6-vps/"],
    "Hop179": ["https://hop179.net/v6-vms.html"],
    "IPv6.rs": ["https://ipv6.rs/"],
}
HOSTS = {urlparse(u).hostname for urls in SOURCES.values() for u in urls}
SEARCHES = [
    '"IPv6 only" "1GB" "year" VPS "$" deal',
    '"IPv6-only" "2GB" "year" VPS "$" sale',
    'site:lowendtalk.com IPv6 only VPS 1GB yearly offer',
    'site:lowendbox.com IPv6 only VPS 1GB annual',
    'site:cloudcone.com IPv6 only 1GB yearly',
]
BLOCKED_WORDS = ("out of stock", "sold out", "no stock", "discontinued",
                 "expired", "promotion ended", "offer ended", "not available")
IPV6_RE = re.compile(r"\bipv6[ -]?only\b|\bonly[ -]?ipv6\b|\bv6[ -]?only\b", re.I)
IPV4_RE = re.compile(r"\b(?:dedicated|included|free|public|one|1|1x|1 x)\s*(?:x\s*)?ipv4\b|\bdual[ -]?stack\b|\bipv4\s*\+\s*ipv6\b", re.I)
RAM_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(GB|GIB|MB|MIB)\s*(?:DDR\d?\s*)?(?:RAM|MEMORY)?\b|\b(?:RAM|MEMORY)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(GB|GIB|MB|MIB)\b", re.I)
PRICE_RE = re.compile(r"(?:USD\s*|US\s*\$\s*|\$\s*)(\d+(?:\.\d{1,2})?)\s*(?:USD\s*)?(?:/|per\s+|a\s+|\s+)(?:year|yr|annum|annually|yearly)\b|\b(?:yearly|annually|annual)\s*(?:price|from|at|:)\s*(?:USD\s*|\$\s*)(\d+(?:\.\d{1,2})?)", re.I)
DATE_RE = re.compile(r"(?:202[0-9])")

def utcnow():
    return datetime.now(timezone.utc)

def normalized(text):
    return re.sub(r"\s+", " ", text).strip()

def ram_mb(text):
    values = []
    for m in RAM_RE.finditer(text):
        n = float(m.group(1) or m.group(3))
        unit = (m.group(2) or m.group(4)).upper()
        if n <= 128:  # Avoid nonsensical page-wide matches.
            values.append(int(n * 1024) if unit in ("GB", "GIB") else int(n))
    return max(values, default=0)

def annual_prices(text):
    return [float(m.group(1) or m.group(2)) for m in PRICE_RE.finditer(text)]

def qualify(text, *, strict=True):
    if not IPV6_RE.search(text) or IPV4_RE.search(text):
        return None
    if any(word in text.lower() for word in BLOCKED_WORDS):
        return None
    ram = ram_mb(text)
    prices = [p for p in annual_prices(text) if 0 < p <= MAX_USD]
    if ram < MIN_RAM_MB or not prices:
        return None
    return {"ram_mb": ram, "annual_usd": min(prices)}

def safe_get(url, allowed_hosts):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ValueError("unapproved external host")
    result = requests.get(url, timeout=TIMEOUT, headers=HEADERS, allow_redirects=True)
    # Never parse an unexpected redirect destination.
    if urlparse(result.url).hostname not in allowed_hosts or urlparse(result.url).scheme != "https":
        raise ValueError("redirect left allowlist")
    result.raise_for_status()
    if len(result.content) > 2_000_000:
        raise ValueError("oversized response")
    return result.text

def card_texts(page):
    soup = BeautifulSoup(page, "html.parser")
    for node in soup(["script", "style", "nav", "footer", "header"]):
        node.decompose()
    seen = set()
    # A plan must have its RAM, IPv6-only status and price in the same compact block.
    for node in soup.select("article, .product, .plan, .pricing-card, .pricing-table, .package, .card, li, tr, section, div"):
        text = normalized(node.get_text(" ", strip=True))
        if not (30 <= len(text) <= 1100) or text in seen:
            continue
        seen.add(text)
        yield text

def key_for(provider, url, price, ram):
    raw = f"{provider}|{url}|{price:.2f}|{ram}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]

def official_scan():
    results, status = {}, []
    for provider, urls in SOURCES.items():
        for url in urls:
            try:
                page = safe_get(url, HOSTS)
                count = 0
                for snippet in card_texts(page):
                    facts = qualify(snippet)
                    if not facts:
                        continue
                    key = key_for(provider, url, facts["annual_usd"], facts["ram_mb"])
                    results[key] = {
                        "key": key, "provider": provider, "url": url,
                        **facts, "confidence": "official-page",
                        "note": snippet[:500],
                        "checked_at": utcnow().isoformat(),
                    }
                    count += 1
                status.append({"provider": provider, "url": url, "ok": True, "matches": count})
            except Exception as exc:
                status.append({"provider": provider, "url": url, "ok": False,
                               "error": str(exc)[:180]})
    return results, status

def search_scan():
    # Search hits are for human review only; never silently treat them as confirmed stock.
    found, status = {}, []
    for query in SEARCHES:
        url = "https://www.bing.com/search?format=rss&q=" + quote_plus(query)
        try:
            rss = safe_get(url, {"www.bing.com", "bing.com"})
            root = ET.fromstring(rss)
            count = 0
            for item in root.findall(".//item")[:30]:
                link = (item.findtext("link") or "").strip()
                parsed = urlparse(link)
                if parsed.scheme != "https" or not parsed.hostname:
                    continue
                # Do not link local or arbitrary search-result domains.
                if parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
                    continue
                title = normalized(item.findtext("title") or "")
                snippet = normalized(BeautifulSoup(item.findtext("description") or "", "html.parser").get_text(" "))
                facts = qualify(title + " " + snippet)
                if not facts:
                    continue
                date_text = item.findtext("pubDate") or ""
                if date_text:
                    from email.utils import parsedate_to_datetime
                    try:
                        published = parsedate_to_datetime(date_text).astimezone(timezone.utc)
                        if published < utcnow() - timedelta(days=21):
                            continue
                    except (TypeError, ValueError, OverflowError):
                        continue
                provider = parsed.hostname.lower()
                key = key_for(provider, link, facts["annual_usd"], facts["ram_mb"])
                found[key] = {"key": key, "provider": provider, "url": link,
                              **facts, "confidence": "search-candidate",
                              "note": (title + " — " + snippet)[:500],
                              "checked_at": utcnow().isoformat()}
                count += 1
            status.append({"query": query, "ok": True, "matches": count})
        except Exception as exc:
            status.append({"query": query, "ok": False, "error": str(exc)[:180]})
    return found, status

def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def main():
    if "--self-test" in sys.argv:
        good = "IPv6-only KVM 1 GB RAM 10 GB SSD $15/year"
        assert qualify(good) == {"ram_mb": 1024, "annual_usd": 15.0}
        assert qualify("IPv6 only 512 MB RAM $10/year") is None
        assert qualify("Dual-stack IPv6 only 1 GB RAM 1 dedicated IPv4 $10/year") is None
        assert qualify("IPv6 only 1 GB RAM $24/year") is None
        assert qualify("IPv6 only 1 GB RAM $1/month") is None
        assert qualify("IPv6 only 1 GB RAM $10/year sold out") is None
        assert qualify("IPv6 only 1 GB RAM $10/year") is not None
        print("IPv6 parser self-test OK")
        return 0

    official, providers = official_scan()
    candidates, search_status = search_scan()
    # Confirmed wins over a search snippet for the same offer URL.
    discovered = {**candidates, **official}
    healthy = any(s["ok"] for s in providers + search_status)
    if not healthy:
        save_json(REPORT, {"checked_at": utcnow().isoformat(), "healthy": False,
                           "sources": providers, "searches": search_status, "new_hits": []})
        print("All sources failed; keep prior state.")
        return 2

    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"seen_keys": []}
    seen = set(state.get("seen_keys", []))
    new_hits = [deal for key, deal in sorted(discovered.items()) if key not in seen]
    # Persist only fresh keys. Stable results do not trigger repeat alerts.
    seen.update(discovered)
    save_json(STATE, {"seen_keys": sorted(seen), "updated_at": utcnow().isoformat()})
    save_json(REPORT, {"checked_at": utcnow().isoformat(), "healthy": healthy,
                       "filters": {"ipv6_only": True, "max_annual_usd": MAX_USD,
                                   "min_ram_mb": MIN_RAM_MB, "regions": "global"},
                       "sources": providers, "searches": search_status,
                       "total_matches": len(discovered), "new_hits": new_hits})
    print(json.dumps({"matches": len(discovered), "new_hits": new_hits},
                     ensure_ascii=False, indent=2))
    return 42 if new_hits else 0

if __name__ == "__main__":
    raise SystemExit(main())
