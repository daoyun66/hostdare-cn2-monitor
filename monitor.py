#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CN2 GIA Multi-Provider Monitor v2.2.0 Stable

Providers:
- HostDare
- DMIT
- BandwagonHost

Hard filter:
- CN2 GIA / CTGNet / DMIT Pro network
- RAM >= 1 GB
- Dedicated IPv4
- Annual price <= 50 USD

DMIT strategy:
1) Direct official DMIT pages (preferred, VERIFIED)
2) Multiple official promo/current pages (preferred, VERIFIED)
3) Bing RSS site-search that only points back to dmit.io (fallback, CANDIDATE)
   - used only when direct DMIT pages are blocked/unreadable
   - candidate must contain all hard-filter evidence in the snippet
   - first run baselines candidates so old indexed pages do not alert

Alerting:
- exit 42 => GitHub Actions intentionally fails => native email
- first healthy run only establishes baseline
- unreadable/403 never means "out of stock"
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, quote_plus

import requests
from bs4 import BeautifulSoup

VERSION = "2.2.0"
MAX_ANNUAL_USD = 50.0
MIN_RAM_MB = 1024
RECENT_ANNOUNCEMENT_DAYS = 60

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

OFFICIAL_HOSTS = {
    "bill.hostdare.com",
    "www.dmit.io", "dmit.io",
    "bandwagonhost.com", "www.bandwagonhost.com",
}
DISCOVERY_HOSTS = {"www.bing.com", "bing.com"}

SOURCES = {
    "hostdare_rss": "https://bill.hostdare.com/announcements/rss",
    "hostdare_cssd": "https://bill.hostdare.com/store/premium-china-optimized-nvme-kvm",
    "hostdare_camd": "https://bill.hostdare.com/store/premium-china-optimized-amd-kvm-vps-usa",

    "dmit_pricing": "https://www.dmit.io/pages/pricing",
    "dmit_pricing_en": "https://www.dmit.io/pages/pricing?language=english",
    "dmit_lax": "https://www.dmit.io/pages/location/los-angeles",
    "dmit_lax_en": "https://www.dmit.io/pages/location/los-angeles?language=english",
    "dmit_announcements": "https://www.dmit.io/index.php?rp=%2Fannouncements",
    "dmit_cloud_instance": "https://www.dmit.io/pages/cloud-instance",
    "dmit_christmas_2026": "https://www.dmit.io/pages/christmas-2026",
    "dmit_blackfriday_2026": "https://www.dmit.io/pages/black-friday-2026",
    "dmit_lax_eyeball": "https://www.dmit.io/pages/lax-eyeball",

    "bandwagon_cart": "https://bandwagonhost.com/cart.php",
}

HOSTDARE_FAMILIES = ("CSSD", "CAMD", "CKVM")

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
    confidence: str = "verified"  # verified | candidate
    coupon: str = ""
    recurring: Optional[bool] = None
    note: str = ""

    @property
    def key(self) -> str:
        raw = "|".join([
            self.provider, self.plan, f"{self.annual_usd:.2f}",
            self.coupon, self.url, self.confidence
        ])
        return "offer:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def qualifies(self) -> bool:
        return (
            self.cn2_gia
            and self.dedicated_ipv4
            and self.ram_mb >= MIN_RAM_MB
            and self.annual_usd <= MAX_ANNUAL_USD
            and (self.orderable or self.confidence == "candidate")
        )

def now_utc():
    return datetime.now(timezone.utc)

def now_iso():
    return now_utc().isoformat()

def clean_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

def cloudflare_block(status: int, text: str, title: str = "") -> bool:
    low = (title + " " + text[:5000]).lower()
    return status in (403, 429, 503) and any(x in low for x in (
        "just a moment", "cloudflare", "attention required",
        "checking your browser", "cf-chl-"
    ))

def fetch(url: str, allow_discovery=False, timeout=25) -> dict:
    host = urlparse(url).hostname or ""
    allowed = host in OFFICIAL_HOSTS or (allow_discovery and host in DISCOVERY_HOSTS)
    out = {
        "url": url, "ok": False, "blocked": False, "status": None,
        "final_url": url, "title": "", "text": "", "html": "", "error": None
    }
    if not allowed:
        out["error"] = f"Host not allowlisted: {host}"
        return out
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        out["status"] = r.status_code
        out["final_url"] = r.url
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        out["title"] = title
        if cloudflare_block(r.status_code, text, title):
            out["blocked"] = True
            out["error"] = "Cloudflare / anti-bot challenge"
            return out
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
            return out
        out["ok"] = True
        out["text"] = text
        out["html"] = r.text
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

def parse_annual_prices(text: str):
    vals = []
    patterns = [
        r"\$\s*(\d+(?:\.\d{1,2})?)\s*(?:USD\s*)?(?:/|\s)\s*(?:year|yr|annually)",
        r"(\d+(?:\.\d{1,2})?)\s*USD\s*(?:/|\s)\s*(?:year|yr|annually)",
        r"\bAnnually\s*\$?\s*(\d+(?:\.\d{1,2})?)",
        r"\b(\d+(?:\.\d{1,2})?)\s*USD\s*/\s*Yr\b",
    ]
    for p in patterns:
        for m in re.finditer(p, text, re.I):
            try:
                vals.append(float(m.group(1)))
            except ValueError:
                pass
    return vals

def parse_ram_mb(text: str):
    pats = [
        r"(\d+(?:\.\d+)?)\s*(GB|MB)\s*(?:ECC\s*)?RAM\b",
        r"\bRAM\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(GB|MB)\b",
    ]
    for p in pats:
        m = re.search(p, text, re.I)
        if m:
            v = float(m.group(1))
            return int(v * 1024) if m.group(2).upper() == "GB" else int(v)

    # Fallback only when text looks like a compact plan card/snippet.
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(GB|MB)\b", text, re.I)
    if m and len(text) < 1800:
        v = float(m.group(1))
        return int(v * 1024) if m.group(2).upper() == "GB" else int(v)
    return None

def has_cn2_gia(text: str):
    low = text.lower()
    return any(x in low for x in (
        "cn2 gia", "cn2-gia", "ctgnet", "as23764", "as4809",
        "premium network"
    ))

def is_dmit_pro(text: str):
    low = text.lower()
    return (
        ".pro." in low
        or " pro " in f" {low} "
        or "premium network" in low
    ) and "tier 1" not in low and ".t1." not in low

def has_dedicated_ipv4(text: str):
    low = text.lower()
    return any(x in low for x in (
        "1 dedicated ipv4", "dedicated ipv4", "1 x ipv4",
        "1 ipv4", "ipv4 &", "ipv4 +"
    ))

def looks_orderable(text: str):
    low = text.lower()
    if any(x in low for x in (
        "out of stock", "sold out", "0 available",
        "currently unavailable", "promotion has ended",
        "promotion is now closed", "event has ended"
    )):
        return False
    return any(x in low for x in (
        "order now", "add to cart", "configure", "continue",
        "buy now", "available"
    ))

def parse_coupon(text: str):
    for p in [
        r"(?:coupon\s*code|promo\s*code|promocode|coupon)\s*[:：]?\s*([A-Z0-9-]{5,80})",
        r"promocode=([A-Z0-9-]{5,80})",
    ]:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1).upper()
    return ""

def parse_recurring(text: str):
    low = text.lower()
    if "recurring" in low:
        return True
    if any(x in low for x in ("first year only", "first payment only", "one-time discount")):
        return False
    return None

# ---------- HostDare ----------

def parse_hostdare():
    url = SOURCES["hostdare_rss"]
    out = {
        "provider": "HostDare", "ok": False, "blocked": False,
        "status": None, "offers": [], "error": None,
        "items_scanned": 0, "recent_items_scanned": 0
    }
    try:
        r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
        out["status"] = r.status_code
        if cloudflare_block(r.status_code, r.text[:5000]):
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
            text = f"{title} {clean_text(desc)}"
            if not has_cn2_gia(text):
                continue
            coupon = parse_coupon(text)
            recurring = parse_recurring(text)

            matches = list(re.finditer(r"\b(CSSD\d+|CAMD\d+|CKVM\d+)\b", text, re.I))
            for i, m in enumerate(matches):
                s = m.start()
                e = matches[i+1].start() if i+1 < len(matches) else min(len(text), s+1800)
                seg = text[s:e]
                ram = parse_ram_mb(seg)
                prices = parse_annual_prices(seg)
                if ram is None or not prices:
                    continue
                plan = m.group(1).upper()
                offer = Offer(
                    "HostDare", plan, min(prices), ram,
                    plan.startswith(HOSTDARE_FAMILIES),
                    True, True, link or url, url,
                    "verified", coupon, recurring,
                    "Recent official HostDare announcement"
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
        rows.append({k:f[k] for k in ("url","ok","blocked","status","title","error")})
    return rows

# ---------- DMIT direct official ----------

def dmit_sections(text: str):
    markers = [
        ("Premium Network", "premium"),
        ("Tier 1 Network", "tier1"),
        ("Eyeball Network", "eyeball"),
    ]
    low = text.lower()
    points = []
    for label, kind in markers:
        start = 0
        needle = label.lower()
        while True:
            idx = low.find(needle, start)
            if idx < 0:
                break
            points.append((idx, label, kind))
            start = idx + len(needle)
    points.sort()
    if not points:
        return [("page", "unknown", text)]
    out = []
    for i, (idx, label, kind) in enumerate(points):
        end = points[i+1][0] if i+1 < len(points) else len(text)
        out.append((label, kind, text[idx:end]))
    return out

def dmit_card_windows(text: str):
    windows = []
    for m in re.finditer(
        r"(?:\$\s*)?(\d+(?:\.\d{1,2})?)\s*(?:USD\s*)?(?:/|\s)\s*(?:Yr|Year|Annually)",
        text, re.I
    ):
        windows.append(text[max(0,m.start()-650):min(len(text),m.end()+650)])
    return windows

def parse_dmit_official_page(name: str, url: str):
    f = fetch(url)
    row = {
        "name": name, "url": url, "ok": f["ok"], "blocked": f["blocked"],
        "status": f["status"], "title": f["title"],
        "offers": [], "error": f["error"]
    }
    if not f["ok"]:
        return row

    text = f["text"]
    for label, kind, section in dmit_sections(text):
        # If explicit section exists, only Premium is accepted.
        if kind not in ("premium", "unknown"):
            continue
        if kind == "unknown" and not is_dmit_pro(section):
            continue
        if not has_cn2_gia(section) and not is_dmit_pro(section):
            continue

        for seg in dmit_card_windows(section):
            if not is_dmit_pro(seg) and kind != "premium":
                continue
            prices = parse_annual_prices(seg)
            ram = parse_ram_mb(seg)
            if not prices or ram is None or not has_dedicated_ipv4(seg):
                continue
            price = min(prices)

            plan = "DMIT Pro"
            pm = re.search(r"\b(PVM\.[A-Z0-9._-]+|LAX\.Pro\.[A-Z0-9._-]+)\b", seg, re.I)
            if pm:
                plan = pm.group(1)

            offer = Offer(
                "DMIT", plan, price, ram, True, True,
                looks_orderable(seg), url, url, "verified",
                note=f"Official DMIT {label} block via {name}"
            )
            if offer.qualifies():
                row["offers"].append(asdict(offer) | {"key": offer.key})
    row["offers"] = list({o["key"]:o for o in row["offers"]}.values())
    return row

def dmit_direct_sources():
    keys = [
        "dmit_pricing", "dmit_pricing_en", "dmit_lax", "dmit_lax_en",
        "dmit_announcements", "dmit_cloud_instance",
        "dmit_christmas_2026", "dmit_blackfriday_2026", "dmit_lax_eyeball"
    ]
    rows = [parse_dmit_official_page(k, SOURCES[k]) for k in keys]
    offers = {}
    for row in rows:
        for o in row["offers"]:
            offers[o["key"]] = o
    return rows, list(offers.values())

# ---------- DMIT discovery fallback ----------

def bing_rss_url(query: str):
    return "https://www.bing.com/search?format=rss&q=" + quote_plus(query)

def parse_bing_rss(query: str):
    url = bing_rss_url(query)
    f = fetch(url, allow_discovery=True)
    row = {
        "query": query, "url": url, "ok": f["ok"],
        "status": f["status"], "items": [], "error": f["error"]
    }
    if not f["ok"]:
        return row

    try:
        root = ET.fromstring(f["html"].encode("utf-8"))
    except Exception:
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            root = ET.fromstring(r.content)
        except Exception as e:
            row["ok"] = False
            row["error"] = f"RSS parse error: {e}"
            return row

    for item in root.findall(".//item")[:20]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = clean_text(item.findtext("description") or "")
        host = (urlparse(link).hostname or "").lower()
        if host not in ("dmit.io", "www.dmit.io"):
            continue
        row["items"].append({"title": title, "link": link, "description": desc})
    return row

def candidate_from_search_item(item: dict):
    text = f"{item['title']} {item['description']}"
    low = text.lower()

    # Strict enough to avoid Tier 1 / historical generic noise.
    if "tier 1" in low or ".t1." in low:
        return None
    if not (is_dmit_pro(text) or "cn2 gia" in low or "ctgnet" in low):
        return None
    if not has_dedicated_ipv4(text):
        return None

    ram = parse_ram_mb(text)
    prices = parse_annual_prices(text)
    if ram is None or not prices:
        return None

    eligible = [p for p in prices if p <= MAX_ANNUAL_USD]
    if not eligible or ram < MIN_RAM_MB:
        return None

    # Do not turn explicitly ended promotions into candidates.
    if any(x in low for x in (
        "promotion has ended", "promotion is now closed",
        "event has ended", "已结束", "已結束"
    )):
        return None

    plan = "DMIT Pro candidate"
    pm = re.search(r"\b(PVM\.[A-Z0-9._-]+|LAX\.Pro\.[A-Z0-9._-]+)\b", text, re.I)
    if pm:
        plan = pm.group(1)

    offer = Offer(
        "DMIT", plan, min(eligible), ram, True, True, False,
        item["link"], "Bing RSS -> official dmit.io result",
        "candidate", note=(
            "Discovery fallback because GitHub Actions cannot read DMIT directly; "
            "all hard-filter evidence appears in search snippet. Open DMIT URL to confirm live stock."
        )
    )
    return (asdict(offer) | {"key": offer.key}) if offer.qualifies() else None

def dmit_discovery():
    queries = [
        'site:dmit.io/pages DMIT "CN2 GIA" "1 IPv4" "USD/Yr" LAX Pro',
        'site:dmit.io/pages "LAX.Pro" "1 GB RAM" "1 IPv4" "USD/Yr"',
        'site:dmit.io/pages DMIT Premium "1 GB RAM" "1 IPv4" Annually',
        'site:dmit.io/pages 2026 DMIT LAX Pro special promotion',
    ]
    rows = [parse_bing_rss(q) for q in queries]
    offers = {}
    for row in rows:
        for item in row["items"]:
            c = candidate_from_search_item(item)
            if c:
                offers[c["key"]] = c
    return rows, list(offers.values())

def parse_dmit():
    direct_rows, direct_offers = dmit_direct_sources()
    direct_healthy = sum(1 for r in direct_rows if r["ok"])

    discovery_rows, candidate_offers = [], []
    # Only use search fallback when every direct official page is unreadable/blocked.
    if direct_healthy == 0:
        discovery_rows, candidate_offers = dmit_discovery()

    discovery_healthy = sum(1 for r in discovery_rows if r.get("ok"))
    offers = direct_offers if direct_healthy else candidate_offers

    return {
        "provider": "DMIT",
        "ok": direct_healthy > 0 or discovery_healthy > 0,
        "mode": (
            "official-direct" if direct_healthy > 0
            else "search-discovery" if discovery_healthy > 0
            else "unavailable"
        ),
        "direct_healthy_sources": direct_healthy,
        "direct_total_sources": len(direct_rows),
        "discovery_healthy_sources": discovery_healthy,
        "discovery_total_sources": len(discovery_rows),
        "direct_sources": direct_rows,
        "discovery_sources": discovery_rows,
        "offers": offers,
        "error": None if (direct_healthy or discovery_healthy) else "No readable DMIT source",
    }

# ---------- BandwagonHost ----------

def parse_bandwagon():
    url = SOURCES["bandwagon_cart"]
    f = fetch(url)
    out = {
        "provider": "BandwagonHost", "ok": f["ok"], "blocked": f["blocked"],
        "status": f["status"], "offers": [], "error": f["error"]
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
            d = hashlib.sha256(text.encode()).hexdigest()
            if d not in seen:
                seen.add(d)
                chunks.append((node,text))
    if not chunks:
        text = f["text"]
        for m in re.finditer(r"Annually", text, re.I):
            chunks.append((None, text[max(0,m.start()-1800):min(len(text),m.end()+800)]))

    for node, seg in chunks:
        if not has_cn2_gia(seg):
            continue
        ram = parse_ram_mb(seg)
        prices = parse_annual_prices(seg)
        if ram is None or not prices or not has_dedicated_ipv4(seg):
            continue

        plan = "BandwagonHost CN2 GIA"
        if node is not None:
            h = node.find(["h1","h2","h3","h4","h5","strong"])
            if h:
                t = re.sub(r"\s+"," ",h.get_text(" ",strip=True))
                if t:
                    plan = t[:180]

        offer = Offer(
            "BandwagonHost", plan, min(prices), ram, True, True,
            looks_orderable(seg), url, url, "verified",
            note="Official BandwagonHost cart"
        )
        if offer.qualifies():
            out["offers"].append(asdict(offer) | {"key": offer.key})
    out["offers"] = list({o["key"]:o for o in out["offers"]}.values())
    return out

# ---------- State ----------

def load_state():
    default = {
        "initialized": False,
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

def save_state(initialized, seen, provider_active):
    STATE_FILE.write_text(json.dumps({
        "initialized": initialized,
        "seen_keys": sorted(seen),
        "provider_active": {k: sorted(v) for k,v in provider_active.items()},
        "updated_at": now_iso(),
    }, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")

def main():
    state = load_state()
    initialized = bool(state["initialized"])
    seen = set(state["seen_keys"])
    old_provider_active = {k:set(v) for k,v in state.get("provider_active",{}).items()}

    hostdare = parse_hostdare()
    hd_health = hostdare_product_health()
    dmit = parse_dmit()
    bandwagon = parse_bandwagon()

    providers = {
        "HostDare": hostdare,
        "DMIT": dmit,
        "BandwagonHost": bandwagon,
    }

    current = {}
    provider_active = {}

    for name, result in providers.items():
        if result.get("ok"):
            keys = set()
            for o in result.get("offers", []):
                keys.add(o["key"])
                current[o["key"]] = o
            provider_active[name] = keys
        else:
            provider_active[name] = old_provider_active.get(name,set())

    healthy_count = sum(1 for x in providers.values() if x.get("ok"))
    baseline_mode = not initialized

    if baseline_mode:
        if healthy_count:
            seen |= set().union(*provider_active.values()) if provider_active else set()
            save_state(True, seen, provider_active)
            new_hits = []
            baseline_created = True
        else:
            save_state(False, seen, old_provider_active)
            new_hits = []
            baseline_created = False
    else:
        new_hits = [o for k,o in current.items() if k not in seen]
        seen |= {o["key"] for o in new_hits}
        save_state(True, seen, provider_active)
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
        "current_qualifying_offers": list(current.values()),
        "providers": {
            "HostDare": hostdare,
            "HostDare_product_pages": hd_health,
            "DMIT": dmit,
            "BandwagonHost": bandwagon,
        },
        "health": {
            "healthy_providers": healthy_count,
            "total_providers": 3,
            "hostdare_ok": hostdare.get("ok"),
            "dmit_ok": dmit.get("ok"),
            "dmit_mode": dmit.get("mode"),
            "dmit_direct_healthy_sources": dmit.get("direct_healthy_sources"),
            "dmit_discovery_healthy_sources": dmit.get("discovery_healthy_sources"),
            "bandwagon_ok": bandwagon.get("ok"),
        }
    }
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if baseline_mode and baseline_created:
        print("\n[BASELINE] Existing qualifying offers/candidates recorded. No historical alert.")
        return 0
    if new_hits:
        print("\n" + "!"*78)
        print("CN2 GIA DEAL FOUND")
        for h in new_hits:
            tag = "VERIFIED" if h["confidence"] == "verified" else "CANDIDATE"
            print(f"- [{tag}] {h['provider']} | {h['plan']} | ${h['annual_usd']:.2f}/yr | {h['ram_mb']}MB")
            print(f"  {h['url']}")
        print("!"*78)
        return 42
    print("\n[OK] No new qualifying offer.")
    return 0

def self_test():
    assert parse_ram_mb("1 GB RAM") == 1024
    assert parse_ram_mb("2048 MB RAM") == 2048
    assert has_dedicated_ipv4("1 IPv4 & 1 IPv6 /64")
    assert has_cn2_gia("China Telecom CN2 GIA Premium Network")
    assert is_dmit_pro("LAX.Pro.WEE CN2 GIA")
    assert not is_dmit_pro("LAX.T1.WEE Tier 1 Network")
    assert min(parse_annual_prices("36.90 USD/Yr")) == 36.90

    good = {
        "title": "DMIT LAX.Pro.WEE CN2 GIA",
        "link": "https://www.dmit.io/pages/example-2026",
        "description": "1 vCPU 1 GB RAM 20 GB SSD 1 IPv4 + 1 IPv6/64 39.90 USD/Yr CN2 GIA Premium"
    }
    cand = candidate_from_search_item(good)
    assert cand is not None and cand["annual_usd"] == 39.90

    bad = {
        "title": "DMIT LAX.T1.WEE",
        "link": "https://www.dmit.io/pages/example",
        "description": "Tier 1 Network 1 GB RAM 1 IPv4 36.90 USD/Yr"
    }
    assert candidate_from_search_item(bad) is None

    print("SELF-TEST OK")
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    raise SystemExit(self_test() if args.self_test else main())
