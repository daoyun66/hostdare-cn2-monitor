#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
HostDare CN2 GIA Monitor v1.1.0

Primary source:
- HostDare official RSS feed (promotion announcements)

Secondary sources:
- Product pages (stock), only when directly readable.
- A Cloudflare 403/Just a moment page is treated as BLOCKED, never as "out of stock".

Alert triggers:
A) New official CN2/CN2 GIA promotion with discount >= MIN_DISCOUNT (default 30%)
B) CSSD1/CAMD1 becomes available on a readable product page

Exit codes:
0  = normal/no new alert
42 = new deal/restock -> GitHub workflow intentionally fails to trigger native email
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

MIN_DISCOUNT = 30

RSS_URL = "https://bill.hostdare.com/announcements/rss"
ANNOUNCEMENTS_URL = "https://bill.hostdare.com/announcements"
PRODUCT_URLS = [
    "https://bill.hostdare.com/store/premium-china-optimized-nvme-kvm",
    "https://bill.hostdare.com/store/premium-china-optimized-amd-kvm-vps-usa",
    "https://bill.hostdare.com/store/premium-china-optimized-kvm-vps",
]

ENTRY_PRODUCTS = ("CSSD1", "CAMD1")

STATE_FILE = Path("state.json")
REPORT_FILE = Path("last_check.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
    "Cache-Control": "no-cache",
}

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def stable_key(prefix: str, value: str) -> str:
    return prefix + ":" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]

def load_state():
    if not STATE_FILE.exists():
        return {"alerted_keys": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"alerted_keys": []}
        data.setdefault("alerted_keys", [])
        return data
    except Exception:
        return {"alerted_keys": []}

def save_state(keys):
    STATE_FILE.write_text(
        json.dumps(
            {"alerted_keys": sorted(set(keys)), "updated_at": now_iso()},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

def html_text(raw: str) -> str:
    soup = BeautifulSoup(raw, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

def is_cloudflare_block(status: int, text: str, title: str = "") -> bool:
    low = (title + " " + text[:2000]).lower()
    return (
        status in (403, 429, 503)
        and any(x in low for x in (
            "just a moment",
            "cloudflare",
            "attention required",
            "checking your browser",
            "cf-chl-",
        ))
    )

def parse_discount(text: str):
    """
    Return highest qualifying discount and coupon code around CN2 context.
    """
    best = None
    low = text.lower()

    # Require CN2/China optimized context somewhere in announcement.
    if "cn2" not in low and "china optimized" not in low:
        return None

    for m in re.finditer(r"(\d{1,2})\s*%\s*(?:off|discount)?", text, re.I):
        pct = int(m.group(1))
        if pct < MIN_DISCOUNT:
            continue
        s = max(0, m.start() - 450)
        e = min(len(text), m.end() + 700)
        window = text[s:e]
        wl = window.lower()

        if "cn2" not in wl and "china optimized" not in wl:
            continue

        recurring = "recurring" in wl
        promoish = any(x in wl for x in ("coupon", "code", "promo", "offer", "discount", "sale"))
        if not (recurring or promoish):
            continue

        code_match = re.search(
            r"(?:coupon\s*code|promo\s*code|code|coupon)\s*[:：]?\s*([A-Z0-9]{5,24})",
            window,
            re.I,
        )
        code = code_match.group(1).upper() if code_match else ""
        item = {
            "discount": pct,
            "recurring": recurring,
            "code": code,
        }
        if best is None or pct > best["discount"]:
            best = item
    return best

def check_rss():
    result = {
        "source": "rss",
        "url": RSS_URL,
        "status": None,
        "ok": False,
        "blocked": False,
        "items_scanned": 0,
        "hits": [],
        "error": None,
    }
    try:
        r = requests.get(RSS_URL, headers=HEADERS, timeout=25, allow_redirects=True)
        result["status"] = r.status_code

        # Some protection layers may return HTML instead of XML.
        ctype = r.headers.get("content-type", "")
        if "html" in ctype.lower():
            t = html_text(r.text)
            title = ""
            try:
                title = BeautifulSoup(r.text, "html.parser").title.get_text(" ", strip=True)
            except Exception:
                pass
            if is_cloudflare_block(r.status_code, t, title):
                result["blocked"] = True
                result["error"] = "RSS endpoint blocked by Cloudflare"
                return result

        r.raise_for_status()
        root = ET.fromstring(r.content)

        # Standard RSS 2.0
        items = root.findall(".//item")
        result["items_scanned"] = len(items)

        for item in items[:80]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or "").strip()
            desc = item.findtext("description") or ""
            pub = (item.findtext("pubDate") or "").strip()

            combined = " ".join([title, html_text(desc)])
            deal = parse_discount(combined)
            if not deal:
                continue

            source_id = guid or link or (title + "|" + pub)
            key = stable_key("rssdeal", f"{source_id}|{deal['discount']}|{deal['code']}")
            result["hits"].append({
                "kind": "discount",
                "key": key,
                "title": title,
                "url": link or ANNOUNCEMENTS_URL,
                "published": pub,
                "discount": deal["discount"],
                "recurring": deal["recurring"],
                "code": deal["code"],
                "detail": (
                    f"{deal['discount']}% CN2/CN2 GIA promotion"
                    + (" / recurring" if deal["recurring"] else "")
                    + (f" / code {deal['code']}" if deal["code"] else "")
                ),
            })

        result["ok"] = True
        return result

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result

def check_product(url: str):
    result = {
        "source": "product",
        "url": url,
        "status": None,
        "ok": False,
        "blocked": False,
        "title": "",
        "hits": [],
        "error": None,
    }
    try:
        r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
        result["status"] = r.status_code

        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        result["title"] = title

        if is_cloudflare_block(r.status_code, text, title):
            result["blocked"] = True
            result["error"] = "Cloudflare blocked this product page; stock state unchanged"
            return result

        if r.status_code != 200:
            result["error"] = f"HTTP {r.status_code}; stock state unchanged"
            return result

        low = text.lower()
        # Ensure this is actually the intended CN2 GIA product page.
        if "cn2 gia" not in low:
            result["error"] = "Readable page but CN2 GIA marker not found; stock state unchanged"
            return result

        for product in ENTRY_PRODUCTS:
            # Find each product occurrence; inspect a reasonably local block.
            for pm in re.finditer(rf"\b{re.escape(product)}\b", text, re.I):
                seg = text[pm.start():pm.start()+1000]
                qty = None
                am = re.search(r"\b(\d+)\s+Available\b", seg, re.I)
                if am:
                    qty = int(am.group(1))

                # Some WHMCS themes use Order Now but no count.
                orderable = bool(re.search(r"(?:Order\s+Now|Add\s+to\s+Cart|Configure)", seg, re.I))
                explicitly_oos = bool(re.search(r"(?:0\s+Available|Out\s+of\s+Stock|Sold\s+Out)", seg, re.I))

                if ((qty is not None and qty > 0) or orderable) and not explicitly_oos:
                    key = f"stock:{product}"
                    result["hits"].append({
                        "kind": "stock",
                        "key": key,
                        "title": f"{product} restock",
                        "url": r.url,
                        "detail": f"{product} appears orderable"
                            + (f" / {qty} Available" if qty is not None else ""),
                    })
                    break

        result["ok"] = True
        return result

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result

def main():
    state = load_state()
    old_keys = set(state.get("alerted_keys", []))

    rss = check_rss()
    products = [check_product(u) for u in PRODUCT_URLS]

    all_hits = []
    if rss["ok"]:
        all_hits.extend(rss["hits"])

    # IMPORTANT:
    # Only stock hits from readable product pages are considered.
    # A blocked product page never clears/redefines stock.
    for p in products:
        if p["ok"]:
            all_hits.extend(p["hits"])

    dedup = {h["key"]: h for h in all_hits}
    current_keys = set(dedup)
    new_hits = [h for k, h in dedup.items() if k not in old_keys]

    # State policy:
    # - RSS deal keys are refreshed from a valid RSS read.
    # - Stock keys are refreshed only when at least one product page is readable.
    # - If product pages are all blocked, retain old stock keys to avoid false reset.
    old_stock = {k for k in old_keys if k.startswith("stock:")}
    old_deals = {k for k in old_keys if not k.startswith("stock:")}

    if rss["ok"]:
        keep_deals = {k for k in current_keys if not k.startswith("stock:")}
    else:
        keep_deals = old_deals

    readable_products = [p for p in products if p["ok"]]
    if readable_products:
        keep_stock = {k for k in current_keys if k.startswith("stock:")}
    else:
        keep_stock = old_stock

    save_state(keep_deals | keep_stock)

    report = {
        "version": "1.1.0",
        "checked_at": now_iso(),
        "minimum_discount": MIN_DISCOUNT,
        "new_hits": new_hits,
        "rss": rss,
        "products": products,
        "health": {
            "rss_ok": rss["ok"],
            "rss_blocked": rss["blocked"],
            "readable_product_pages": len(readable_products),
            "blocked_product_pages": sum(1 for p in products if p["blocked"]),
        },
    }
    REPORT_FILE.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if new_hits:
        print("\n" + "!" * 72)
        print("🚨 HostDare CN2 GIA NEW DEAL / RESTOCK")
        for h in new_hits:
            print(f"- {h['detail']}")
            print(f"  {h['url']}")
        print("!" * 72)
        return 42

    # Monitor is still considered successful when product pages are blocked,
    # as long as RSS works. This avoids false failure emails due solely to Cloudflare.
    if not rss["ok"] and not readable_products:
        print("\n[WARN] All sources currently unreadable; no stock/deal decision made.")
    else:
        print("\n[OK] No new qualifying deal/restock.")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
