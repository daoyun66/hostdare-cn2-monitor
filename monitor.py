#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CN2 GIA Multi-Provider Monitor v2.0.0

Providers:
- HostDare
- DMIT
- BandwagonHost

Hard criteria:
- CN2 GIA / CTGNet confirmed by the same official source
- RAM >= 1 GB
- Dedicated IPv4
- Annual price <= 50 USD
- Offer is currently orderable / newly announced

Safety:
- Official domains only
- Cloudflare / unreadable pages never mean "out of stock"
- First healthy run establishes a baseline and does not alert
- Later new qualifying offers return exit code 42
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

VERSION = "2.0.0"

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
    "bandwagon_cart": "https://bandwagonhost.com/cart.php",
}

ALLOWED_HOSTS = {
    "bill.hostdare.com",
    "www.dmit.io",
    "dmit.io",
    "bandwagonhost.com",
    "www.bandwagonhost.com",
}

# HostDare product families known to include one dedicated IPv4 on their official product pages.
# We still require the announcement/product text to confirm CN2 GIA and the price/RAM.
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
            self.provider,
            self.plan,
            f"{self.annual_usd:.2f}",
            self.coupon,
            self.url,
        ])
        return "offer:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def qualifies(self) -> bool:
        return (
            self.cn2_gia
            and self.dedicated_ipv4
            and self.orderable
            and self.ram_mb >= MIN_RAM_MB
            and self.annual_usd <= MAX_ANNUAL_USD
        )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
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
    low = (title + " " + body[:4000]).lower()
    markers = (
        "just a moment",
        "cloudflare",
        "attention required",
        "checking your browser",
        "cf-chl-",
    )
    return status in (403, 429, 503) and any(x in low for x in markers)


def fetch(url: str, timeout: int = 25) -> dict:
    result = {
        "url": url,
        "ok": False,
        "blocked": False,
        "status": None,
        "final_url": url,
        "title": "",
        "text": "",
        "html": "",
        "error": None,
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


def parse_usd_annual(text: str) -> list[float]:
    vals = []
    patterns = [
        r"\$\s*(\d+(?:\.\d{1,2})?)\s*(?:USD\s*)?[/ ]\s*(?:year|yr|annually)",
        r"\$\s*(\d+(?:\.\d{1,2})?)\s*USD\s*(?:/year|annually)",
        r"(\d+(?:\.\d{1,2})?)\s*USD\s*(?:/year|annually)",
        r"\$\s*(\d+(?:\.\d{1,2})?)\s*USD\s*Annually",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            try:
                vals.append(float(m.group(1)))
            except Exception:
                pass
    return vals


def parse_ram_mb(text: str) -> Optional[int]:
    # Prefer an explicit "RAM" expression.
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)\s*(?:ECC\s*)?RAM\b", text, re.I)
    if not m:
        m = re.search(r"\bRAM\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(GB|MB)\b", text, re.I)
    if not m:
        # Common DMIT cards show "1GB" or "2GB" without "RAM".
        m = re.search(r"\b(\d+(?:\.\d+)?)\s*GB\b", text, re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).upper() if len(m.groups()) >= 2 else "GB"
    return int(val * 1024) if unit == "GB" else int(val)


def has_cn2_gia(text: str) -> bool:
    low = text.lower()
    return (
        "cn2 gia" in low
        or "cn2-gia" in low
        or "ctgnet" in low
        or "as4809" in low
        or "as23764" in low
    )


def has_dedicated_ipv4(text: str) -> bool:
    low = text.lower()
    patterns = (
        "1 dedicated ipv4",
        "1 dedicated ipv4 address",
        "ipv4: 1 dedicated",
        "ipv4 1 dedicated",
        "1 x ipv4",
        "1 ipv4",
        "dedicated ipv4",
    )
    return any(p in low for p in patterns)


def looks_orderable(text: str) -> bool:
    low = text.lower()
    bad = ("out of stock", "sold out", "0 available", "currently unavailable")
    if any(x in low for x in bad):
        return False
    good = ("order now", "add to cart", "configure", "continue", "buy now", "available")
    return any(x in low for x in good)


def parse_coupon(text: str) -> str:
    patterns = [
        r"(?:coupon\s*code|promo\s*code|promocode|coupon)\s*[:：]?\s*([A-Z0-9]{5,30})",
        r"promocode=([A-Z0-9]{5,30})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).upper()
    return ""


def parse_recurring(text: str) -> Optional[bool]:
    low = text.lower()
    if "recurring" in low:
        return True
    if "first year only" in low or "first payment only" in low or "one-time discount" in low:
        return False
    return None


def parse_hostdare_rss() -> dict:
    url = SOURCES["hostdare_rss"]
    out = {
        "provider": "HostDare",
        "source": url,
        "ok": False,
        "blocked": False,
        "status": None,
        "items_scanned": 0,
        "recent_items_scanned": 0,
        "offers": [],
        "error": None,
    }

    try:
        r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
        out["status"] = r.status_code
        body_text = r.text[:5000]
        if blocked_by_cloudflare(r.status_code, body_text):
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
                # If date cannot be parsed, skip instead of risking a historical false alert.
                continue

            out["recent_items_scanned"] += 1
            text = f"{title} {normalize_text(desc)}"

            if not has_cn2_gia(text):
                continue

            coupon = parse_coupon(text)
            recurring = parse_recurring(text)

            # Parse plan sections such as:
            # CSSD1 ... 1 GB RAM ... $36.39/year
            family_pat = r"\b(CSSD\d+|CAMD\d+|CKVM\d+)\b"
            matches = list(re.finditer(family_pat, text, re.I))
            for i, m in enumerate(matches):
                start = m.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), start + 1800)
                seg = text[start:end]
                plan = m.group(1).upper()

                ram = parse_ram_mb(seg)
                prices = parse_usd_annual(seg)
                if ram is None or not prices:
                    continue

                price = min(prices)
                # HostDare's official KVM China-optimized families include a dedicated IPv4.
                dedicated = plan.startswith(HOSTDARE_DEDICATED_IPV4_FAMILIES)

                order_url = link or url
                um = re.search(r"https?://bill\.hostdare\.com/[^\s<>\"']+", seg, re.I)
                if um:
                    order_url = um.group(0).rstrip(".,;)")

                offer = Offer(
                    provider="HostDare",
                    plan=plan,
                    annual_usd=price,
                    ram_mb=ram,
                    dedicated_ipv4=dedicated,
                    cn2_gia=True,
                    orderable=True,  # New recent official promo announcement is treated as live.
                    url=order_url,
                    source=url,
                    coupon=coupon,
                    recurring=recurring,
                    note=f"Official announcement within {RECENT_ANNOUNCEMENT_DAYS} days",
                )
                if offer.qualifies():
                    out["offers"].append(asdict(offer) | {"key": offer.key})

        out["ok"] = True
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


def parse_hostdare_product_pages() -> dict:
    """
    Product pages are used primarily for health/stock corroboration.
    They are often Cloudflare-blocked from GitHub Actions, so failure here
    does not invalidate the RSS monitor.
    """
    results = []
    for key in ("hostdare_cssd", "hostdare_camd"):
        f = fetch(SOURCES[key])
        results.append({
            "url": f["url"],
            "ok": f["ok"],
            "blocked": f["blocked"],
            "status": f["status"],
            "title": f["title"],
            "error": f["error"],
        })
    return {"pages": results}


def extract_candidate_windows(text: str, annual_marker=r"Annually") -> list[str]:
    windows = []
    for m in re.finditer(annual_marker, text, re.I):
        s = max(0, m.start() - 1800)
        e = min(len(text), m.end() + 800)
        windows.append(text[s:e])
    return windows


def parse_dmit() -> dict:
    url = SOURCES["dmit_pricing"]
    f = fetch(url)
    out = {
        "provider": "DMIT",
        "source": url,
        "ok": f["ok"],
        "blocked": f["blocked"],
        "status": f["status"],
        "offers": [],
        "error": f["error"],
    }
    if not f["ok"]:
        return out

    text = f["text"]

    # Critical anti-false-positive rule:
    # DMIT has Tier 1 (T1) products that are NOT China-specific.
    # Only a local window that itself says CN2 GIA / CTGNet is accepted.
    for seg in extract_candidate_windows(text, annual_marker=r"Annually"):
        if not has_cn2_gia(seg):
            continue

        prices = parse_usd_annual(seg)
        ram = parse_ram_mb(seg)
        if not prices or ram is None:
            continue

        # Try to capture the closest plan label before the annual price.
        pm = re.findall(r"\b([A-Z][A-Z0-9._-]{2,30})\b", seg)
        plan = pm[-1] if pm else "DMIT-PREMIUM"

        dedicated = has_dedicated_ipv4(seg)
        # If the card doesn't repeat IPv4 wording, do not infer it.
        if not dedicated:
            continue

        orderable = looks_orderable(seg)
        price = min(prices)

        offer = Offer(
            provider="DMIT",
            plan=plan,
            annual_usd=price,
            ram_mb=ram,
            dedicated_ipv4=True,
            cn2_gia=True,
            orderable=orderable,
            url=url,
            source=url,
            note="Accepted only when the local pricing block confirms CN2 GIA and dedicated IPv4",
        )
        if offer.qualifies():
            out["offers"].append(asdict(offer) | {"key": offer.key})

    return out


def parse_bandwagon() -> dict:
    url = SOURCES["bandwagon_cart"]
    f = fetch(url)
    out = {
        "provider": "BandwagonHost",
        "source": url,
        "ok": f["ok"],
        "blocked": f["blocked"],
        "status": f["status"],
        "offers": [],
        "error": f["error"],
    }
    if not f["ok"]:
        return out

    soup = BeautifulSoup(f["html"], "html.parser")

    # WHMCS product cards vary by template. Gather medium-sized text containers
    # that have an annual price and then deduplicate by content.
    chunks = []
    selectors = [
        ".product",
        ".product-info",
        ".package",
        ".package-name",
        ".panel",
        ".card",
        ".products .product",
        "form",
    ]
    seen = set()
    for sel in selectors:
        for node in soup.select(sel):
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
            if len(text) < 80 or len(text) > 7000:
                continue
            if "Annually" not in text and "/year" not in text.lower():
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest not in seen:
                seen.add(digest)
                chunks.append((node, text))

    # Fallback to annual-price windows if template selectors did not work.
    if not chunks:
        for seg in extract_candidate_windows(f["text"], annual_marker=r"Annually"):
            chunks.append((None, seg))

    for node, seg in chunks:
        if not has_cn2_gia(seg):
            continue

        ram = parse_ram_mb(seg)
        prices = parse_usd_annual(seg)
        if ram is None or not prices:
            continue

        dedicated = has_dedicated_ipv4(seg)
        if not dedicated:
            continue

        price = min(prices)
        orderable = looks_orderable(seg)

        # Product name: prefer a heading; otherwise first text before "SSD:".
        plan = "BandwagonHost CN2 GIA"
        if node is not None:
            heading = node.find(["h1", "h2", "h3", "h4", "h5", "strong"])
            if heading:
                candidate = re.sub(r"\s+", " ", heading.get_text(" ", strip=True))
                if candidate:
                    plan = candidate[:180]
        if plan == "BandwagonHost CN2 GIA":
            m = re.search(r"([A-Z0-9][A-Z0-9 ._-]{5,120}(?:VPS|PROMO|BOX|PLAN))", seg, re.I)
            if m:
                plan = m.group(1).strip()[:180]

        offer = Offer(
            provider="BandwagonHost",
            plan=plan,
            annual_usd=price,
            ram_mb=ram,
            dedicated_ipv4=True,
            cn2_gia=True,
            orderable=orderable,
            url=url,
            source=url,
            note="Official BandwagonHost cart page",
        )
        if offer.qualifies():
            out["offers"].append(asdict(offer) | {"key": offer.key})

    # Deduplicate identical keys.
    unique = {}
    for o in out["offers"]:
        unique[o["key"]] = o
    out["offers"] = list(unique.values())
    return out


def load_state() -> dict:
    default = {
        "initialized": False,
        "active_keys": [],
        "seen_keys": [],
        "updated_at": None,
    }
    if not STATE_FILE.exists():
        return default
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return default


def save_state(initialized: bool, active_keys: set[str], seen_keys: set[str]) -> None:
    data = {
        "initialized": initialized,
        "active_keys": sorted(active_keys),
        "seen_keys": sorted(seen_keys),
        "updated_at": now_iso(),
    }
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def provider_healthy(result: dict) -> bool:
    return bool(result.get("ok"))


def main() -> int:
    state = load_state()
    initialized = bool(state.get("initialized"))
    old_active = set(state.get("active_keys", []))
    seen = set(state.get("seen_keys", []))

    hostdare = parse_hostdare_rss()
    hostdare_pages = parse_hostdare_product_pages()
    dmit = parse_dmit()
    bandwagon = parse_bandwagon()

    providers = [hostdare, dmit, bandwagon]

    current_by_provider = {}
    all_current = {}
    for p in providers:
        provider = p["provider"]
        offers = p.get("offers", [])
        keys = {o["key"] for o in offers}
        current_by_provider[provider] = keys
        for o in offers:
            all_current[o["key"]] = o

    # Preserve previous active keys for unreadable providers so a transient block
    # cannot look like "sold out" and then create a false restock later.
    effective_active = set()
    provider_prefix_map = {
        "HostDare": None,
        "DMIT": None,
        "BandwagonHost": None,
    }

    # We don't encode provider name directly in the hash key, so preserve old keys
    # only when a provider is unreadable by using seen/current semantics conservatively.
    # If any provider is unreadable, old active keys remain in effective_active.
    if any(not provider_healthy(p) for p in providers):
        effective_active |= old_active

    for keys in current_by_provider.values():
        effective_active |= keys

    healthy_count = sum(1 for p in providers if provider_healthy(p))
    baseline_mode = not initialized

    if baseline_mode:
        if healthy_count >= 1:
            seen |= effective_active
            save_state(True, effective_active, seen)
            new_hits = []
            baseline_created = True
        else:
            save_state(False, old_active, seen)
            new_hits = []
            baseline_created = False
    else:
        # Alert only when an offer is currently qualifying and we have never alerted/seen it.
        new_keys = {k for k in effective_active if k in all_current and k not in seen}
        new_hits = [all_current[k] for k in sorted(new_keys)]
        seen |= new_keys
        # Keep previously seen keys forever to avoid repeat alerts.
        save_state(True, effective_active, seen)
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
        "initialized_before_run": initialized,
        "new_hits": new_hits,
        "current_qualifying_offers": list(all_current.values()),
        "providers": {
            "HostDare": hostdare,
            "HostDare_product_pages": hostdare_pages,
            "DMIT": dmit,
            "BandwagonHost": bandwagon,
        },
        "health": {
            "healthy_provider_sources": healthy_count,
            "total_provider_sources": len(providers),
            "hostdare_rss_ok": hostdare.get("ok"),
            "dmit_ok": dmit.get("ok"),
            "bandwagon_ok": bandwagon.get("ok"),
        },
    }

    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if baseline_mode and baseline_created:
        print("\n[BASELINE] Existing qualifying offers recorded. No email alert on initialization.")
        return 0

    if new_hits:
        print("\n" + "!" * 78)
        print("CN2 GIA DEAL FOUND")
        for h in new_hits:
            print(
                f"- {h['provider']} | {h['plan']} | "
                f"${h['annual_usd']:.2f}/yr | RAM {h['ram_mb']} MB"
            )
            if h.get("coupon"):
                print(f"  Coupon: {h['coupon']}")
            print(f"  {h['url']}")
        print("!" * 78)
        return 42

    if healthy_count == 0:
        print("\n[WARN] All provider sources unreadable. State preserved; no alert.")
    else:
        print("\n[OK] No new qualifying offer.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
