#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CN2 GIA Multi-Provider Monitor v2.1.0

Providers:
- HostDare
- DMIT
- BandwagonHost

Hard criteria:
- CN2 GIA / CTGNet confirmed by official source
- RAM >= 1 GB
- Dedicated IPv4
- Annual price <= 50 USD
- Currently orderable / newly announced

v2.1.0 highlights:
- DMIT multi-source official fallback:
  * pricing
  * pricing?language=english
  * Los Angeles datacenter page
  * Los Angeles page?language=english
  * announcements
- DMIT network-section parser prevents Tier 1 WEE ($36.90/yr) false positive.
- Last-known-good DMIT snapshot preserved while all DMIT sources are blocked.
- Provider/source health matrix in Summary.
- GitHub Actions upgraded to checkout@v5 / setup-python@v6 and ubuntu-24.04.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

VERSION = "2.1.0"
MAX_ANNUAL_USD = 50.00
MIN_RAM_MB = 1024
RECENT_ANNOUNCEMENT_DAYS = 45

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

SOURCES = {
    "hostdare_rss": "https://bill.hostdare.com/announcements/rss",
    "hostdare_cssd": "https://bill.hostdare.com/store/premium-china-optimized-nvme-kvm",
    "hostdare_camd": "https://bill.hostdare.com/store/premium-china-optimized-amd-kvm-vps-usa",

    "dmit_pricing": "https://www.dmit.io/pages/pricing",
    "dmit_pricing_en": "https://www.dmit.io/pages/pricing?language=english",
    "dmit_lax": "https://www.dmit.io/pages/location/los-angeles",
    "dmit_lax_en": "https://www.dmit.io/pages/location/los-angeles?language=english",
    "dmit_announcements": "https://www.dmit.io/index.php?rp=%2Fannouncements",

    "bandwagon_cart": "https://bandwagonhost.com/cart.php",
}

ALLOWED_HOSTS = {
    "bill.hostdare.com",
    "www.dmit.io", "dmit.io",
    "bandwagonhost.com", "www.bandwagonhost.com",
}

HOSTDARE_DEDICATED_IPV4_FAMILIES = ("CSSD", "CAMD", "CKVM")

@dataclass(frozen=True)
class Offer:
    provider: str
    plan: str
    annual_usd: float
    ram_mb: int
    dedicated_ipv4: bool
    cn2_gia: bool
    orderable: bool
    url: str
    source: str
    coupon: str = ""
    recurring: Optional[bool] = None
    note: str = ""

    @property
    def key(self) -> str:
        raw = "|".join([
            self.provider, self.plan, f"{self.annual_usd:.2f}",
            self.coupon, self.url
        ])
        return "offer:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def qualifies(self) -> bool:
        return (
            self.cn2_gia and self.dedicated_ipv4 and self.orderable
            and self.ram_mb >= MIN_RAM_MB
            and self.annual_usd <= MAX_ANNUAL_USD
        )

def now_utc():
    return datetime.now(timezone.utc)

def now_iso():
    return now_utc().isoformat()

def allowed_url(url: str) -> bool:
    try:
        return urlparse(url).hostname in ALLOWED_HOSTS
    except Exception:
        return False

def normalize_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

def blocked_by_cloudflare(status: int, body: str, title: str = "") -> bool:
    low = (title + " " + body[:5000]).lower()
    return (
        status in (403, 429, 503)
        and any(x in low for x in (
            "just a moment", "cloudflare", "attention required",
            "checking your browser", "cf-chl-"
        ))
    )

def fetch(url: str, timeout=25) -> dict:
    result = {
        "url": url, "ok": False, "blocked": False, "status": None,
        "final_url": url, "title": "", "text": "", "html": "",
        "error": None
    }
    if not allowed_url(url):
        result["error"] = "URL host is not allowlisted"
        return result
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        result["status"] = r.status_code
        result["final_url"] = r.url
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        result["title"] = title
        if blocked_by_cloudflare(r.status_code, text, title):
            result["blocked"] = True
            result["error"] = "Cloudflare / anti-bot challenge"
            return result
        if r.status_code != 200:
            result["error"] = f"HTTP {r.status_code}"
            return result
        result["ok"] = True
        result["text"] = text
        result["html"] = r.text
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result

def parse_usd_annual(text: str):
    vals = []
    pats = (
        r"\$\s*(\d+(?:\.\d{1,2})?)\s*(?:USD\s*)?[/ ]\s*(?:year|yr|annually)",
        r"\$\s*(\d+(?:\.\d{1,2})?)\s*USD\s*(?:/year|annually)",
        r"(\d+(?:\.\d{1,2})?)\s*USD\s*(?:/year|annually)",
        r"\$\s*(\d+(?:\.\d{1,2})?)\s*/\s*Annually",
    )
    for pat in pats:
        for m in re.finditer(pat, text, re.I):
            try:
                vals.append(float(m.group(1)))
            except Exception:
                pass
    return vals

def parse_ram_mb(text: str):
    patterns = (
        r"(\d+(?:\.\d+)?)\s*(GB|MB)\s*(?:ECC\s*)?RAM\b",
        r"\bRAM\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(GB|MB)\b",
        r"\b(\d+(?:\.\d+)?)\s*(GB|MB)\b",
    )
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            val = float(m.group(1))
            return int(val * 1024) if m.group(2).upper() == "GB" else int(val)
    return None

def has_cn2_gia(text: str):
    low = text.lower()
    return any(x in low for x in ("cn2 gia", "cn2-gia", "ctgnet", "as23764", "as4809"))

def has_dedicated_ipv4(text: str):
    low = text.lower()
    return any(x in low for x in (
        "1 dedicated ipv4", "dedicated ipv4", "1 x ipv4",
        "1 ipv4", "1 ipv4 &", "1 ipv4 +"
    ))

def looks_orderable(text: str):
    low = text.lower()
    if any(x in low for x in ("out of stock", "sold out", "0 available", "currently unavailable")):
        return False
    return any(x in low for x in ("order now", "add to cart", "configure", "continue", "buy now", "available"))

def parse_coupon(text: str):
    for pat in (
        r"(?:coupon\s*code|promo\s*code|promocode|coupon)\s*[:：]?\s*([A-Z0-9]{5,30})",
        r"promocode=([A-Z0-9]{5,30})",
    ):
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).upper()
    return ""

def parse_recurring(text: str):
    low = text.lower()
    if "recurring" in low:
        return True
    if "first year only" in low or "first payment only" in low or "one-time discount" in low:
        return False
    return None

def parse_hostdare_rss():
    url = SOURCES["hostdare_rss"]
    out = {
        "provider": "HostDare", "source": url, "ok": False, "blocked": False,
        "status": None, "items_scanned": 0, "recent_items_scanned": 0,
        "offers": [], "error": None
    }
    try:
        r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
        out["status"] = r.status_code
        if blocked_by_cloudflare(r.status_code, r.text[:5000]):
            out["blocked"] = True
            out["error"] = "RSS blocked by Cloudflare"
            return out
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = root.findall(".//item")
        out["items_scanned"] = len(items)
        cutoff = now_utc() - timedelta(days=RECENT_ANNOUNCEMENT_DAYS)

        for item in items[:100]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = item.findtext("description") or ""
            pub = (item.findtext("pubDate") or "").strip()
            try:
                dt = email.utils.parsedate_to_datetime(pub)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except Exception:
                continue

            out["recent_items_scanned"] += 1
            text = f"{title} {normalize_text(desc)}"
            if not has_cn2_gia(text):
                continue

            coupon = parse_coupon(text)
            recurring = parse_recurring(text)
            matches = list(re.finditer(r"\b(CSSD\d+|CAMD\d+|CKVM\d+)\b", text, re.I))
            for i, m in enumerate(matches):
                start = m.start()
                end = matches[i+1].start() if i+1 < len(matches) else min(len(text), start+1800)
                seg = text[start:end]
                plan = m.group(1).upper()
                ram = parse_ram_mb(seg)
                prices = parse_usd_annual(seg)
                if ram is None or not prices:
                    continue
                price = min(prices)
                dedicated = plan.startswith(HOSTDARE_DEDICATED_IPV4_FAMILIES)
                offer = Offer(
                    "HostDare", plan, price, ram, dedicated, True, True,
                    link or url, url, coupon, recurring,
                    f"Official announcement within {RECENT_ANNOUNCEMENT_DAYS} days"
                )
                if offer.qualifies():
                    out["offers"].append(asdict(offer) | {"key": offer.key})
        out["ok"] = True
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

def hostdare_product_health():
    rows = []
    for key in ("hostdare_cssd", "hostdare_camd"):
        f = fetch(SOURCES[key])
        rows.append({k: f[k] for k in ("url","ok","blocked","status","title","error")})
    return {"pages": rows}

# ---------------- DMIT v2.1.0 ----------------

def dmit_network_sections(text: str):
    """
    Split official DMIT content into named network sections.
    This is the key anti-false-positive layer:
    a cheap Tier 1 plan must never inherit "CN2 GIA" text from another section.
    """
    markers = [
        ("Premium Network", "premium"),
        ("Premium 網路", "premium"),
        ("Tier 1 Network", "tier1"),
        ("Tier 1 網路", "tier1"),
        ("Eyeball Network", "eyeball"),
        ("Eyeball 網路", "eyeball"),
    ]
    points = []
    low = text.lower()
    for label, kind in markers:
        pos = 0
        needle = label.lower()
        while True:
            idx = low.find(needle, pos)
            if idx < 0:
                break
            points.append((idx, label, kind))
            pos = idx + len(needle)
    points.sort()

    sections = []
    for i, (idx, label, kind) in enumerate(points):
        end = points[i+1][0] if i+1 < len(points) else len(text)
        seg = text[idx:end]
        sections.append((label, kind, seg))
    return sections

def dmit_offer_windows(section: str):
    # Annual plans are the only ones relevant to <= $50/year.
    windows = []
    for m in re.finditer(r"\$\s*(\d+(?:\.\d{1,2})?)\s*/?\s*Annually|(\d+(?:\.\d{1,2})?)\s*USD\s*Annually", section, re.I):
        s = max(0, m.start()-500)
        e = min(len(section), m.end()+500)
        windows.append(section[s:e])
    return windows

def parse_dmit_page(source_name: str, url: str):
    f = fetch(url)
    row = {
        "name": source_name, "url": url, "ok": f["ok"], "blocked": f["blocked"],
        "status": f["status"], "title": f["title"], "offers": [], "error": f["error"]
    }
    if not f["ok"]:
        return row

    for label, kind, section in dmit_network_sections(f["text"]):
        if kind != "premium":
            continue

        # Premium section itself must explicitly say CN2 GIA/CTGNet.
        if not has_cn2_gia(section):
            continue

        for seg in dmit_offer_windows(section):
            prices = parse_usd_annual(seg)
            ram = parse_ram_mb(seg)
            if not prices or ram is None:
                continue
            price = min(prices)

            # DMIT official product cards usually say "1 IPv4 & 1 IPv6 /64".
            dedicated = has_dedicated_ipv4(seg)
            if not dedicated:
                continue

            # Plan name nearest to this card.
            plan = "DMIT Premium"
            # Capture likely plan IDs / names before resources.
            candidates = re.findall(r"\b(?:PVM\.[A-Z0-9._-]+|[A-Z][A-Z0-9._-]{2,32})\b", seg)
            skip = {"GB","SSD","USD","RAM","CN2","GIA","IPV4","IPV6","MAX","OUT","IN"}
            candidates = [x for x in candidates if x.upper() not in skip]
            if candidates:
                plan = candidates[-1]

            orderable = looks_orderable(seg)
            offer = Offer(
                "DMIT", plan, price, ram, True, True, orderable,
                url, url, note=f"Official {label} section from {source_name}"
            )
            if offer.qualifies():
                row["offers"].append(asdict(offer) | {"key": offer.key})
    # dedupe
    row["offers"] = list({o["key"]: o for o in row["offers"]}.values())
    return row

def parse_dmit_announcements():
    url = SOURCES["dmit_announcements"]
    f = fetch(url)
    return {
        "name": "announcements", "url": url, "ok": f["ok"], "blocked": f["blocked"],
        "status": f["status"], "title": f["title"], "offers": [],
        "error": f["error"]
    }

def parse_dmit_multi():
    names = (
        ("pricing", SOURCES["dmit_pricing"]),
        ("pricing_en", SOURCES["dmit_pricing_en"]),
        ("lax", SOURCES["dmit_lax"]),
        ("lax_en", SOURCES["dmit_lax_en"]),
    )
    source_rows = [parse_dmit_page(n, u) for n, u in names]
    source_rows.append(parse_dmit_announcements())

    all_offers = {}
    for row in source_rows:
        for o in row["offers"]:
            all_offers[o["key"]] = o

    healthy = [r for r in source_rows if r["ok"]]
    return {
        "provider": "DMIT",
        "ok": bool(healthy),
        "degraded": bool(healthy) and len(healthy) < len(source_rows),
        "healthy_sources": len(healthy),
        "total_sources": len(source_rows),
        "sources": source_rows,
        "offers": list(all_offers.values()),
        "error": None if healthy else "All official DMIT sources are unreadable",
    }

# ---------------- BandwagonHost ----------------

def parse_bandwagon():
    url = SOURCES["bandwagon_cart"]
    f = fetch(url)
    out = {
        "provider": "BandwagonHost", "source": url, "ok": f["ok"],
        "blocked": f["blocked"], "status": f["status"], "offers": [],
        "error": f["error"]
    }
    if not f["ok"]:
        return out

    soup = BeautifulSoup(f["html"], "html.parser")
    chunks, seen = [], set()
    for sel in (".product",".product-info",".package",".panel",".card",".products .product","form"):
        for node in soup.select(sel):
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
            if len(text) < 80 or len(text) > 7000:
                continue
            if "Annually" not in text and "/year" not in text.lower():
                continue
            dig = hashlib.sha256(text.encode()).hexdigest()
            if dig not in seen:
                seen.add(dig)
                chunks.append((node, text))

    if not chunks:
        text = f["text"]
        for m in re.finditer(r"Annually", text, re.I):
            chunks.append((None, text[max(0,m.start()-1800):min(len(text),m.end()+800)]))

    for node, seg in chunks:
        if not has_cn2_gia(seg):
            continue
        ram = parse_ram_mb(seg)
        prices = parse_usd_annual(seg)
        if ram is None or not prices or not has_dedicated_ipv4(seg):
            continue
        price = min(prices)
        plan = "BandwagonHost CN2 GIA"
        if node is not None:
            h = node.find(["h1","h2","h3","h4","h5","strong"])
            if h:
                candidate = re.sub(r"\s+", " ", h.get_text(" ", strip=True))
                if candidate:
                    plan = candidate[:180]
        offer = Offer(
            "BandwagonHost", plan, price, ram, True, True,
            looks_orderable(seg), url, url, note="Official BandwagonHost cart page"
        )
        if offer.qualifies():
            out["offers"].append(asdict(offer) | {"key": offer.key})
    out["offers"] = list({o["key"]: o for o in out["offers"]}.values())
    return out

# ---------------- State / alerting ----------------

def load_state():
    default = {
        "initialized": False,
        "active_keys": [],
        "seen_keys": [],
        "provider_active": {},
        "updated_at": None,
    }
    if not STATE_FILE.exists():
        return default
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default
        for k,v in default.items():
            data.setdefault(k,v)
        return data
    except Exception:
        return default

def save_state(initialized, active_keys, seen_keys, provider_active):
    STATE_FILE.write_text(json.dumps({
        "initialized": initialized,
        "active_keys": sorted(active_keys),
        "seen_keys": sorted(seen_keys),
        "provider_active": {k: sorted(v) for k,v in provider_active.items()},
        "updated_at": now_iso(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def main():
    state = load_state()
    initialized = bool(state["initialized"])
    seen = set(state["seen_keys"])
    old_provider_active = {k:set(v) for k,v in state.get("provider_active",{}).items()}

    hostdare = parse_hostdare_rss()
    hd_pages = hostdare_product_health()
    dmit = parse_dmit_multi()
    bandwagon = parse_bandwagon()

    providers = {
        "HostDare": hostdare,
        "DMIT": dmit,
        "BandwagonHost": bandwagon,
    }

    all_current = {}
    provider_active = {}

    for name, result in providers.items():
        if result.get("ok"):
            keys = set()
            for o in result.get("offers", []):
                keys.add(o["key"])
                all_current[o["key"]] = o
            provider_active[name] = keys
        else:
            # Preserve last-known-good state while provider is unreadable.
            provider_active[name] = old_provider_active.get(name, set())

    effective_active = set().union(*provider_active.values()) if provider_active else set()
    healthy_count = sum(1 for x in providers.values() if x.get("ok"))
    baseline_mode = not initialized

    if baseline_mode:
        if healthy_count >= 1:
            seen |= effective_active
            save_state(True, effective_active, seen, provider_active)
            new_hits, baseline_created = [], True
        else:
            save_state(False, set(state["active_keys"]), seen, old_provider_active)
            new_hits, baseline_created = [], False
    else:
        new_keys = {k for k in effective_active if k in all_current and k not in seen}
        new_hits = [all_current[k] for k in sorted(new_keys)]
        seen |= new_keys
        save_state(True, effective_active, seen, provider_active)
        baseline_created = False

    report = {
        "version": VERSION,
        "checked_at": now_iso(),
        "criteria": {
            "cn2_gia_required": True,
            "min_ram_mb": MIN_RAM_MB,
            "dedicated_ipv4_required": True,
            "max_annual_usd": MAX_ANNUAL_USD,
        },
        "baseline_mode": baseline_mode,
        "baseline_created": baseline_created,
        "new_hits": new_hits,
        "current_qualifying_offers": list(all_current.values()),
        "providers": {
            "HostDare": hostdare,
            "HostDare_product_pages": hd_pages,
            "DMIT": dmit,
            "BandwagonHost": bandwagon,
        },
        "health": {
            "healthy_provider_sources": healthy_count,
            "total_provider_sources": 3,
            "hostdare_ok": hostdare.get("ok"),
            "dmit_ok": dmit.get("ok"),
            "dmit_healthy_official_sources": dmit.get("healthy_sources"),
            "dmit_total_official_sources": dmit.get("total_sources"),
            "bandwagon_ok": bandwagon.get("ok"),
        }
    }
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if baseline_mode and baseline_created:
        print("\n[BASELINE] Existing qualifying offers recorded; no historical alert.")
        return 0
    if new_hits:
        print("\n" + "!"*78)
        print("CN2 GIA DEAL FOUND")
        for h in new_hits:
            print(f"- {h['provider']} | {h['plan']} | ${h['annual_usd']:.2f}/yr | RAM {h['ram_mb']}MB")
            print(f"  {h['url']}")
        print("!"*78)
        return 42
    if healthy_count == 0:
        print("\n[WARN] All providers unreadable; state preserved.")
    else:
        print("\n[OK] No new qualifying offer.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
