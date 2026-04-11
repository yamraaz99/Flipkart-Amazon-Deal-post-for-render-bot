#!/usr/bin/env python3
"""
Deal Post Bot v7 — Multi-Template Edition (Standard & Optimized)
  • Added Historical Price API for Regular Price extraction
  • Added Interactive Inline Button to toggle templates
  • Artifact-proof fast image cropping
"""

import os
import re
import json
import logging
import asyncio
import base64
from io import BytesIO
from urllib.parse import urlparse
import fitz

from weasyprint import HTML
import httpx
import requests
from bs4 import BeautifulSoup
from keep_alive import keep_alive
from fake_useragent import UserAgent
from PIL import Image as PILImage
from jinja2 import Template

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# curl_cffi for Flipkart TLS fingerprinting
try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    _HAS_CFFI = False
    logging.warning("curl_cffi not installed — Flipkart scraping may fail")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
EXT_ID = "7242722"
EXT_AUTH = "788970602"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

SHORT_DOMAINS =[
    "amzn.to", "amzn.in", "bit.ly",
    "fkrt.site", "fkrt.cc", "fkrt.co", "fkrt.to",
    "dl.flipkart.com",
]

_BANK_RE = re.compile(
    r"((?:SBI|HDFC|ICICI|Axis|Kotak|RBL|HSBC|Yes\sBank|IndusInd|Federal|"
    r"BOB|Citi|AMEX|Amazon\sPay|OneCard|AU|Flipkart\sAxis|BOBCARD)"
    r"(?:\sBank)?\s*(?:Credit|Debit)?\s*Card[s]?)",
    re.I,
)

BANK_COLORS = {
    "sbi": "#0d6efd", "hdfc": "#004b8d", "icici": "#f37920",
    "axis": "#97144d", "kotak": "#ed1c24", "rbl": "#21409a",
    "hsbc": "#db0011", "yes bank": "#0066b3", "indusind": "#8b1a4a",
    "federal": "#f7a800", "bob": "#f47920", "citi": "#003ea4",
    "amex": "#006fcf", "amazon pay": "#ff9900", "onecard": "#000000",
    "au": "#ec1c24", "flipkart axis": "#2874f0", "bobcard": "#f47920",
}


def _get_bank_color(bank_name):
    name = bank_name.lower()
    for key, color in BANK_COLORS.items():
        if key in name:
            return color
    return "#666666"


# ────────────────────────────────────────────────────────────────────
# 1. URL HANDLING
# ────────────────────────────────────────────────────────────────────
def resolve_url(url):
    domain = urlparse(url).netloc
    if any(sd in domain for sd in SHORT_DOMAINS):
        try:
            r = requests.get(
                url, allow_redirects=True, timeout=10, stream=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            u = r.url
            r.close()
            return u
        except Exception:
            pass
    return url


def detect_marketplace(url):
    if "amazon" in url or "amzn" in url:
        m = re.search(r"(?:/dp/|/gp/product/)([A-Z0-9]{10})", url)
        if m:
            return "amazon", m.group(1), 63
    elif "flipkart" in url or "fkrt" in url:
        m = re.search(r"(?:pid=|/p/)([A-Za-z0-9]{16})", url)
        if m:
            return "flipkart", m.group(1), 2
    return None, None, None


def make_clean_url(mkt, pid, url):
    if mkt == "amazon":
        tld = re.search(r"amazon\.([a-z.]+)", url)
        return f"https://www.amazon.{tld.group(1) if tld else 'in'}/dp/{pid}"
    return url


# ────────────────────────────────────────────────────────────────────
# 2. HEADERS
# ────────────────────────────────────────────────────────────────────
def _desktop_headers():
    ua = UserAgent(
        fallback="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/126.0.0.0 Safari/537.36"
    )
    return {
        "User-Agent": ua.random,
        "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.google.com/",
        "Upgrade-Insecure-Requests": "1",
    }


def _mobile_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Mobile Safari/537.36"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.google.com/",
        "Upgrade-Insecure-Requests": "1",
    }


def _clean_price(txt):
    if not txt:
        return None
    c = re.sub(r"[^\d.]", "", str(txt).split(".")[0])
    try:
        return int(c) if c else None
    except ValueError:
        return None


# ────────────────────────────────────────────────────────────────────
# 3. BUYHATKE & HISTORICAL APIs
# ────────────────────────────────────────────────────────────────────
async def api_product_details(url):
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"https://ext1.buyhatke.com/extension-apis/chatBot/"
                f"fetchProductDetails?extId={EXT_ID}&extAuth={EXT_AUTH}",
                json={"url": url},
                headers={"Content-Type": "application/json"},
            )
            d = r.json()
            return d.get("data", {}) if d.get("status") == 1 else {}
    except Exception as e:
        log.error(f"api_product_details: {e}")
        return {}


async def api_thunder(pid, pos):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "https://ext1.buyhatke.com/extension-apis/thunder/getPidData",
                json={"pos": pos, "pids": [pid]},
                headers={"Content-Type": "application/json"},
            )
            d = r.json()
            if d.get("status"):
                raw = d.get("data", {})
                entry = raw.get(f"{pos}:{pid}", raw)
                if isinstance(entry, str):
                    entry = json.loads(entry)
                return entry if isinstance(entry, dict) else {}
    except Exception as e:
        log.error(f"api_thunder: {e}")
    return {}


async def api_compare(pid, pos):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://search-new.bitbns.com/buyhatke/comparePrice",
                params={"PID": pid, "pos": pos, "trst": 1},
            )
            return r.json().get("data",[])
    except Exception as e:
        log.error(f"api_compare: {e}")
    return[]


async def get_historical_regular_price(pid, pos):
    """Fetches historical price data to establish a 'Regular Price'"""
    url = f"https://graph.bitbns.com/getPredictedData.php?type=log&indexName=interest_centers&logName=info&pos={pos}&pid={pid}&mainFL=1"
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(url)
            if r.status_code == 200 and "~*~*" in r.text:
                parts = r.text.split("~*~*")
                prices =[]
                for p in parts:
                    if "~" in p:
                        try:
                            # Safely extract the price, cleaning trailing & symbols
                            price_str = p.split("~")[1].split("&")[0].strip()
                            val = int(price_str)
                            if val > 0:
                                prices.append(val)
                        except:
                            pass
                if prices:
                    # Return the true average historical price
                    return sum(prices) // len(prices)
    except Exception as e:
        log.error(f"Error fetching regular price: {e}")
    return 0


# ────────────────────────────────────────────────────────────────────
# 4. BANK OFFER EXTRACTION & SCRAPERS (UNCHANGED)
# ────────────────────────────────────────────────────────────────────
def _extract_bank_offers_amazon(soup):
    offers =[]
    seen = set()
    for card in soup.select("#poExpander .a-carousel-card, #ppd .a-carousel-card, .a-carousel-card, [data-feature-name=\"buyNowFitWidget\"] .a-box,[data-feature-name=\"buyNowFit498Widget\"] .a-box"):
        text = card.get_text(" ", strip=True)
        buy_match = re.search(r"Buy\s+for\s*(?:₹|Rs\.?)\s*([\d,]+)", text, re.I)
        if not buy_match: continue
        final_price = int(buy_match.group(1).replace(",", ""))
        coupon_match = re.search(r"Coupon\s*[-−]?\s*(?:₹|Rs\.?)\s*([\d,]+)", text, re.I)
        coupon_amt = int(coupon_match.group(1).replace(",", "")) if coupon_match else 0
        bank_match = _BANK_RE.search(text)
        if not bank_match: continue
        bank_name = bank_match.group(1).strip()
        if bank_name.lower() in seen: continue
        seen.add(bank_name.lower())
        bank_disc_match = re.search(re.escape(bank_name) + r".*?[-−]\s*(?:₹|Rs\.?)\s*([\d,]+)", text, re.I)
        bank_disc = int(bank_disc_match.group(1).replace(",", "")) if bank_disc_match else 0
        is_emi = bool(re.search(r"\bEMI\b", text, re.I))
        offers.append({"bank": bank_name, "discount_flat": bank_disc, "coupon_in_card": coupon_amt, "final_price": final_price, "is_emi": is_emi, "text": text[:150]})
    selectors = "#poExpander li, #soWidget li, #itembox-InstallmentCalculator li,[data-csa-c-content-id*=\"offer\"] li, .a-unordered-list .a-list-item"
    for item in soup.select(selectors):
        txt = item.get_text(" ", strip=True)
        if len(txt) < 15 or len(txt) > 400: continue
        bm = _BANK_RE.search(txt)
        if not bm: continue
        bank = bm.group(1).strip()
        if bank.lower() in seen: continue
        seen.add(bank.lower())
        offer = {"bank": bank, "text": txt[:150], "is_emi": False}
        pct = re.search(r"(\d+)\s*%\s*(?:instant\s*)?(?:discount|off|cashback|savings)", txt, re.I)
        flat = re.search(r"(?:₹|Rs\.?|INR)\s*([\d,]+)\s*(?:instant\s*)?(?:discount|off|cashback|savings)", txt, re.I)
        cap = re.search(r"(?:up\s*to|upto|max\.?)\s*(?:₹|Rs\.?|INR)\s*([\d,]+)", txt, re.I)
        if pct: offer["discount_pct"] = int(pct.group(1))
        if flat: offer["discount_flat"] = int(flat.group(1).replace(",", ""))
        if cap: offer["max_discount"] = int(cap.group(1).replace(",", ""))
        if re.search(r"\bEMI\b", txt, re.I): offer["is_emi"] = True
        offers.append(offer)
    return offers

def _extract_flipkart_bank_offers_json(html_text):
    pattern = re.compile(r'\{"type":"NepOffers","bankCardType":"BANK_OFFER_PILL"')
    offers =[]
    seen = set()
    for match in pattern.finditer(html_text):
        fragment = html_text[match.start():]
        depth, end_idx = 0, -1
        for i, ch in enumerate(fragment[:10000]):
            if ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0: end_idx = i; break
        if end_idx == -1: continue
        try: obj = json.loads(fragment[: end_idx + 1])
        except (json.JSONDecodeError, ValueError): continue
        bank = obj.get("offerTitle", "").strip()
        discount_text = obj.get("discountedPriceText", "").strip()
        if not bank or not discount_text: continue
        card_type = ""
        try:
            content_list = obj["offerSubTitleRC"]["value"]["contentList"]
            card_type = " • ".join(x["contentValue"] for x in content_list if x.get("contentType") == "TEXT")
        except (KeyError, TypeError): pass
        card_type_clean = card_type.split("•")[0].strip() if card_type else ""
        full_bank = f"{bank} {card_type_clean}".strip() if card_type_clean else bank
        dedup_key = full_bank.lower()
        if dedup_key in seen: continue
        seen.add(dedup_key)
        disc_match = re.search(r"[\d,]+", discount_text.replace("₹", ""))
        disc_amt = int(disc_match.group().replace(",", "")) if disc_match else 0
        if disc_amt <= 0: continue
        is_emi = bool(re.search(r"\bemi\b", card_type, re.I))
        offers.append({"bank": full_bank, "discount_flat": disc_amt, "is_emi": is_emi, "text": f"{discount_text} {bank} {card_type}"[:150]})
    offers.sort(key=lambda x: x.get("discount_flat", 0), reverse=True)
    return offers

def scrape_amazon(url):
    result = {"current_price": None, "mrp": None, "coupon": None, "bank_offers":[]}
    try:
        s = requests.Session()
        s.headers.update(_desktop_headers())
        resp = s.get(url, timeout=8)
        soup = BeautifulSoup(resp.content, "html.parser")
        if "captcha" not in resp.text.lower()[:2000]:
            for sel in[".priceToPay .a-price-whole", ".a-price .a-offscreen", "#priceblock_ourprice", "#priceblock_dealprice", "#corePriceDisplay_desktop_feature_div .a-price-whole", "span.a-price-whole"]:
                el = soup.select_one(sel)
                if el:
                    p = _clean_price(el.get_text())
                    if p and p > 0: result["current_price"] = p; break
            for sel in[".a-price.a-text-price .a-offscreen", ".basisPrice .a-offscreen", "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen"]:
                el = soup.select_one(sel)
                if el:
                    m = _clean_price(el.get_text())
                    if m and m > 0: result["mrp"] = m; break
            if not result["mrp"]: result["mrp"] = result["current_price"]
            for sel in["#coupons-card-sub-heading-before-apply", 'label[id^="couponText"]', ".promoPriceBlockMessage", "#couponBadgeRegularVpc"]:
                el = soup.select_one(sel)
                if el:
                    txt = el.get_text(strip=True)
                    if any(w in txt.lower() for w in ["coupon", "save", "%", "₹"]):
                        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", txt)
                        flat = re.search(r"(?:₹|Rs\.?)\s*(\d[\d,]*)", txt, re.I)
                        if pct: result["coupon"] = {"type": "percent", "value": float(pct.group(1)), "text": txt}
                        elif flat: result["coupon"] = {"type": "flat", "value": int(flat.group(1).replace(",", "")), "text": txt}
                        break
            if not result["coupon"]:
                for lbl in soup.find_all("label"):
                    t = lbl.get_text(strip=True)
                    if "coupon" in t.lower() and ("apply" in t.lower() or "save" in t.lower()):
                        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
                        flat = re.search(r"(?:₹|Rs\.?)\s*(\d[\d,]*)", t, re.I)
                        if pct: result["coupon"] = {"type": "percent", "value": float(pct.group(1)), "text": t}
                        elif flat: result["coupon"] = {"type": "flat", "value": int(flat.group(1).replace(",", "")), "text": t}
                        break
            result["bank_offers"] = _extract_bank_offers_amazon(soup)
    except Exception: pass
    if len(result["bank_offers"]) < 2:
        try:
            s2 = requests.Session()
            s2.headers.update(_mobile_headers())
            resp2 = s2.get(url, timeout=8)
            soup2 = BeautifulSoup(resp2.content, "html.parser")
            if "captcha" not in resp2.text.lower()[:2000]:
                mobile_offers = _extract_bank_offers_amazon(soup2)
                existing = {o["bank"].lower() for o in result["bank_offers"]}
                for o in mobile_offers:
                    if o["bank"].lower() not in existing: result["bank_offers"].append(o)
                if not result["current_price"]:
                    for sel in[".a-price .a-offscreen", "#newPrice .a-offscreen", 'span[data-a-color="price"] .a-offscreen']:
                        el = soup2.select_one(sel)
                        if el:
                            p = _clean_price(el.get_text())
                            if p and p > 0: result["current_price"] = p; break
        except Exception: pass
    return result

def _fetch_flipkart_html(url):
    if _HAS_CFFI:
        try:
            sess = cffi_requests.Session(impersonate="chrome120")
            try:
                resp = sess.get(url, timeout=10)
                if resp.status_code == 200 and len(resp.text) > 5000:
                    html = resp.text
                    sess.close()
                    return html
            except Exception: pass
            sess.close()
        except Exception: pass
    try:
        s = requests.Session()
        s.headers.update(_desktop_headers())
        resp = s.get(url, timeout=8)
        if resp.status_code == 200 and len(resp.text) > 5000: return resp.text
    except Exception: pass
    return ""

def scrape_flipkart(url):
    result = {"current_price": None, "mrp": None, "coupon": None, "bank_offers":[]}
    html_text = _fetch_flipkart_html(url)
    if not html_text: return result
    soup = BeautifulSoup(html_text, "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text)
            if isinstance(data, list): data = data[0]
            if data.get("@type") == "Product":
                offers = data.get("offers", {})
                if isinstance(offers, list) and offers: result["current_price"] = _clean_price(str(offers[0].get("price")))
                elif isinstance(offers, dict): result["current_price"] = _clean_price(str(offers.get("price")))
        except Exception: continue
    if not result["current_price"]:
        for pat in[r'"sellingPrice"\s*:\s*(\d+)', r'"finalPrice"\s*:\s*(\d+)']:
            m = re.search(pat, html_text)
            if m:
                val = int(m.group(1))
                if val > 0: result["current_price"] = val; break
    for sel in["div.yRaY8j", "div._3I9_wc"]:
        el = soup.select_one(sel)
        if el: result["mrp"] = _clean_price(el.get_text()); break
    if not result["mrp"]:
        for pat in[r'"mrp"\s*:\s*(\d+)', r'"maximumRetailPrice"\s*:\s*(\d+)']:
            m = re.search(pat, html_text)
            if m:
                val = int(m.group(1))
                if val > 0: result["mrp"] = val; break
    if not result["mrp"]: result["mrp"] = result["current_price"]
    result["bank_offers"] = _extract_flipkart_bank_offers_json(html_text)
    return result

async def shorten_title_groq(full_title):
    if not GROQ_API_KEY: return full_title
    if len(full_title) <= 70: return full_title
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages":[
                        {"role": "system", "content": "You shorten e-commerce product titles. Keep: brand, key specs (size, capacity, star rating, color), product type. Remove: model codes, marketing buzzwords, AI features, pipe-separated feature lists, processor names. Max ~80 characters. Return ONLY the title, nothing else."},
                        {"role": "user", "content": full_title},
                    ],
                    "temperature": 0, "max_tokens": 100,
                },
            )
            data = resp.json()
            shortened = data["choices"][0]["message"]["content"].strip().strip('"').strip("'")
            if shortened and len(shortened) > 10: return shortened
    except Exception: pass
    return full_title

def calc_breakdown(price, mrp, coupon, bank_offers):
    b = {"mrp": mrp or price or 0, "price": price or 0, "coupon_disc": 0, "coupon_text": None, "after_coupon": price or 0, "best_bank": None, "best_bank_disc": 0, "best_bank_is_emi": False, "effective": price or 0}
    if not price: return b
    if coupon:
        if coupon["type"] == "percent":
            b["coupon_disc"] = int(price * coupon["value"] / 100)
            b["coupon_text"] = f"Apply {int(coupon['value'])}% Coupon on page"
        else:
            b["coupon_disc"] = int(coupon["value"])
            b["coupon_text"] = f"Apply ₹{int(coupon['value']):,} Coupon on page"
        b["after_coupon"] = price - b["coupon_disc"]
    ap = b["after_coupon"]
    for o in bank_offers:
        d = 0
        if o.get("final_price"):
            d = ap - o["final_price"]
            if d < 0: d = 0
        elif "discount_flat" in o: d = o["discount_flat"]
        elif "discount_pct" in o:
            d = int(ap * o["discount_pct"] / 100)
            if "max_discount" in o: d = min(d, o["max_discount"])
        if d > b["best_bank_disc"]:
            b["best_bank_disc"] = d
            b["best_bank"] = o["bank"]
            b["best_bank_is_emi"] = o.get("is_emi", False)
    b["effective"] = ap - b["best_bank_disc"]
    return b


# ────────────────────────────────────────────────────────────────────
# 7. HTML TEMPLATES (Standard & Optimized)
# ────────────────────────────────────────────────────────────────────

OPTIMIZED_DEAL_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Perfected Marketplace Price Breakdown Banner</title>
<style>
  /* Give WeasyPrint a generous fixed canvas, the threshold-cropper will slice off the empty bottom automatically! */
  @page {
    size: 1040px 1000px; 
    margin: 0;
  }

  /* Base Reset & Variables securely injected from Python */
  :root {
    --bg-color: {{ bg_color }};
    --theme-color: {{ theme_color }};
    --tag-color: {{ tag_color }};
    --text-white: #ffffff;
  }

  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }

  body {
    font-family: "Amazon Ember", "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    /* Pure white background allows the Pillow cropper to cut the edges flawlessly */
    background-color: #ffffff; 
    padding: 30px;
  }

  /* --- Main Theme Banner --- */
  .banner-container {
    background-color: var(--bg-color);
    background-image: repeating-linear-gradient(
      45deg,
      rgba(0, 0, 0, 0.02) 0px,
      rgba(0, 0, 0, 0.02) 2px,
      transparent 2px,
      transparent 8px
    );
    padding: 35px;
    display: flex;
    gap: 30px;
    width: 980px; 
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.15);
  }

  /* --- LEFT COLUMN: Product Image Box --- */
  .product-image-container {
    flex: 1;
    background-color: #ffffff;
    display: flex;
    justify-content: center;
    align-items: center;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
    min-height: 420px; 
    padding: 20px;
  }

  .product-image-container img {
    max-width: 100%;
    max-height: 380px;
    object-fit: contain;
  }

  /* --- RIGHT COLUMN: Info Structure --- */
  .info-container {
    flex: 1.25; 
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  /* Shared Theme Box Styling */
  .teal-box {
    background-color: var(--theme-color);
    color: var(--text-white);
    box-shadow: 0 3px 6px rgba(0, 0, 0, 0.08);
    border-radius: 4px;
  }

  /* 1. MARKETPLACE Header Box */
  .marketplace-box {
    align-self: center;
    width: 65%; 
    text-align: center;
    padding: 12px 20px;
    font-size: 28px;
    font-weight: 700;
    letter-spacing: 0.5px;
    position: relative;
  }

  /* Custom Inline SVG for the Discount Tag (%) */
  .discount-tag {
    position: absolute;
    top: -18px;
    right: -28px;
    width: 58px;
    height: 58px;
    filter: drop-shadow(2px 4px 4px rgba(0,0,0,0.25));
    transform: rotate(15deg);
    z-index: 10;
  }

  /* 2. PRODUCT TITLE Box */
  .title-box {
    text-align: center;
    padding: 16px 20px;
    font-size: 20px;
    font-weight: 600;
    line-height: 1.4;
  }

  /* 3. Regular Price Box */
  .price-box {
    text-align: center;
    padding: 14px 20px;
    font-size: 19px;
    font-weight: 500;
  }

  /* 4. Checkout Price Breakdown Box */
  .breakdown-box {
    flex-grow: 1; 
    padding: 24px 35px;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }

  .breakdown-row {
    display: flex;
    justify-content: space-between;
    font-size: 22px;
    margin-bottom: 12px;
    letter-spacing: 0.3px;
  }

  .breakdown-row span:last-child {
    font-weight: 400; 
  }

  /* Added a subtle green color to savings row so it pops beautifully against the teal/blue */
  .savings-row {
    color: #a3e6a3; 
  }

  .divider {
    height: 2px;
    background-color: #ffffff;
    margin: 18px 0;
  }

  .order-total {
    font-size: 26px;
    font-weight: 700;
    margin-bottom: 0;
  }
</style>
</head>
<body>

<div class="banner-container">
  
  <!-- LEFT: Product Image Card -->
  <div class="product-image-container">
    <img src="data:image/jpeg;base64,{{ img_b64 }}" alt="product">
  </div>

  <!-- RIGHT: Pricing Information Stack -->
  <div class="info-container">
    
    <!-- Header -->
    <div class="teal-box marketplace-box">
      {{ marketplace_name }}
      
      <!-- SVG Tag -->
      {% if discount_pct > 0 %}
      <svg class="discount-tag" viewBox="0 0 100 100">
        <path d="M 20 5 L 85 5 L 85 85 L 20 85 L 0 45 Z" fill="var(--tag-color)" />
        <circle cx="18" cy="45" r="6" fill="var(--bg-color)"/> 
        <!-- Dynamic Percentage Variable Injected -->
        <text x="54" y="60" fill="#ffffff" font-size="34" font-weight="bold" font-family="Arial, sans-serif" text-anchor="middle">{{ discount_pct }}%</text>
      </svg>
      {% endif %}
    </div>

    <!-- Title -->
    <div class="teal-box title-box">
      {{ product_title }}
    </div>

    <!-- Retail Price (Only rendered if avg price is >= 20% higher than current price) -->
    {% if show_regular_price %}
    <div class="teal-box price-box">
      Regular Price: &#8377;{{ regular_price_fmt }} ({{ regular_discount_pct }}% OFF)
    </div>
    {% endif %}

    <!-- Breakdown Bottom Block -->
    <div class="teal-box breakdown-box">
      <div class="breakdown-row">
        <span>Items:</span>
        <span>&#8377;{{ price_fmt }}</span>
      </div>
      <div class="breakdown-row">
        <span>Delivery:</span>
        <span>&#8377;0.00</span>
      </div>
      
      <!-- Dynamic Savings Loop -->
      {% for saving in savings %}
      <div class="breakdown-row savings-row">
        <span>{{ saving.label }}:</span>
        <span>&minus;&#8377;{{ saving.value }}</span>
      </div>
      {% endfor %}
      
      <div class="divider"></div>
      
      <div class="breakdown-row order-total">
        <span>Order Total:</span>
        <span>&#8377;{{ effective_fmt }}</span>
      </div>
    </div>

  </div>
</div>

</body>
</html>"""
)


AMAZON_DEAL_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
@page { size: {{ canvas_width }}px 1200px; margin: 0; }
*{margin:0;padding:0;box-sizing:border-box}
body{ font-family:"Amazon Ember",Arial,sans-serif; background:#fff; width:{{ canvas_width }}px; padding:{{ pad }}px; }
.card{ display:flex; {% if layout == 'stack' %} flex-direction:column;align-items:center; {% else %} flex-direction:row;align-items:flex-start;gap:28px; {% endif %} }
.img-box{ {% if layout == 'stack' %}text-align:center;margin-bottom:24px; {% else %}flex-shrink:0;{% endif %} }
.img-box img{max-width:{{ img_max }}px;max-height:{{ img_max }}px;object-fit:contain}
.info-panel{flex:1;min-width:0;width:100%}
.cpn-card { background: #fff; border: 1px solid #e7e7e7; border-radius: 8px; padding: 16px 20px; margin-bottom: 20px; overflow: hidden; }
.cpn-icon { float: left; width: 34px; margin-right: 15px; margin-top: 10px; }
.cpn-icon svg { width: 34px; height: 24px; display: block; }
.cpn-txt { float: left; }
.cpn-title { font-size: 22px; font-weight: 700; color: #0f1111; line-height: 1; margin-bottom: 6px; }
.cpn-desc { font-size: 19px; color: #333; line-height: 1.2; }
.cpn-green { background-color: #7ddc67; color: #0f1111; padding: 2px 4px; margin-left: -2px; }
.cpn-right { float: right; margin-top: 3px; }
.cpn-btn { background: #fff; border: 1px solid #8d9096; border-radius: 8px; padding: 8px 18px; font-size: 18px; color: #0f1111; display: inline-block; text-align: center; }
.pb{color:#0f1111;font-size:16px;padding:0 12px}
.pb-r{display:flex;justify-content:space-between;margin-bottom:9px;line-height:1.2}
.pb-blue{color:#007185}
.pb-green{color:#007600}
.pb-box{border:4px solid #fa5a4f;padding:6px 8px;margin:4px -12px}
.pb-box .pb-r{margin-bottom:9px}
.pb-box .pb-r:last-child{margin-bottom:0}
.pb-div{border-top:1.5px solid #0f1111;margin:12px 0 10px 0}
.pb-total{font-size:20px;font-weight:700;margin-top:10px}
.pb-caret{ display:inline-block;width:6px;height:6px; border-left:2px solid #0f1111;border-top:2px solid #0f1111; transform:rotate(45deg);margin-left:6px; vertical-align:middle;position:relative;top:-2px; }
</style>
</head>
<body>
<div class="card">
  <div class="img-box"><img src="data:image/jpeg;base64,{{ img_b64 }}" alt="product"></div>
  <div class="info-panel">
    {% if coupon_disc > 0 %}
    <div class="cpn-card">
      <div class="cpn-icon">
        <svg width="34" height="24" viewBox="0 0 34 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M 3 3 L 31 3 L 21 12 L 31 21 L 3 21 Z" stroke="#565656" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
          <text x="13" y="16.5" fill="#f08800" font-family="Arial,sans-serif" font-weight="bold" font-size="14" text-anchor="middle">&#8377;</text>
        </svg>
      </div>
      <div class="cpn-txt">
        <div class="cpn-title">Coupon Discount</div>
        <div class="cpn-desc"><span class="cpn-green">Save &#8377;{{ coupon_disc_fmt }}</span> with coupon</div>
      </div>
      <div class="cpn-right"><div class="cpn-btn">Apply</div></div>
    </div>
    {% endif %}
    <div class="pb">
      <div class="pb-r"><span>Items:</span><span>&#8377;{{ price_fmt }}.00</span></div>
      <div class="pb-r"><span>Delivery:</span><span>&#8377;0.00</span></div>
      <div class="pb-r"><span>Total:</span><span>&#8377;{{ price_fmt }}.00</span></div>
      {% if savings_count > 0 %}
      <div class="pb-box">
        <div class="pb-r"><span class="pb-blue">Savings ({{ savings_count }}):<span class="pb-caret"></span></span><span class="pb-green">&minus;&#8377;{{ total_savings_fmt }}.00</span></div>
        {% if best_bank_disc > 0 %}<div class="pb-r"><span>{{ best_bank }} Discount:</span><span>&minus;&#8377;{{ best_bank_disc_fmt }}.00</span></div>{% endif %}
        {% if coupon_disc > 0 %}<div class="pb-r"><span>Your Coupon Savings</span><span>&minus;&#8377;{{ coupon_disc_fmt }}.00</span></div>{% endif %}
      </div>
      {% endif %}
      <div class="pb-div"></div>
      <div class="pb-r pb-total"><span>Order Total:</span><span>&#8377;{{ effective_fmt }}.00</span></div>
    </div>
  </div>
</div>
</body>
</html>"""
)

FLIPKART_DEAL_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
@page { size: {{ canvas_width }}px 1200px; margin: 0; }
*{margin:0;padding:0;box-sizing:border-box}
body{ font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; background:#fff; width:{{ canvas_width }}px; padding:{{ pad }}px; }
.card{ display:flex; {% if layout == 'stack' %} flex-direction:column;align-items:center; {% else %} flex-direction:row;align-items:flex-start;gap:28px; {% endif %} }
.img-box{ {% if layout == 'stack' %}text-align:center;margin-bottom:24px; {% else %}flex-shrink:0;{% endif %} }
.img-box img{max-width:{{ img_max }}px;max-height:{{ img_max }}px;object-fit:contain}
.info-panel{flex:1;min-width:0;width:100%}
.fk{ background:#f4f5fa;padding:24px 0 0 0; font-size:16px;color:#212121;border-radius:10px;overflow:hidden; }
.fk-r{ display:flex;justify-content:space-between;align-items:center; padding:0 20px;margin-bottom:24px; }
.fk-gray{color:#6b7280} .fk-green{color:#0b9e4d} .fk-blue{color:#2874f0} .fk-bold-blue{color:#2874f0;font-weight:700;font-size:18px}
.fk-caret{ display:inline-block;width:7px;height:7px; border-left:1.5px solid #212121;border-top:1.5px solid #212121; transform:rotate(45deg);margin-left:6px;position:relative;top:-2px; }
.fk-caret-blue{border-color:#2874f0}
.fk-hbox{ border:4px solid #f84537;padding-top:18px;padding-bottom:12px; }
.fk-hbox .fk-r{margin-bottom:18px} .fk-hbox .fk-r:last-child{margin-bottom:0}
.fk-div{border-top:1px solid #e0e2e7;margin:18px 20px 16px 20px}
</style>
</head>
<body>
<div class="card">
  <div class="img-box"><img src="data:image/jpeg;base64,{{ img_b64 }}" alt="product"></div>
  <div class="info-panel">
    <div class="fk">
      <div class="fk-r"><span>MRP (incl. of all taxes)</span><span>&#8377;{{ mrp_fmt }}</span></div>
      {% if has_any_discount %}
      <div class="fk-r" style="margin-bottom:12px"><span>Discounts <span class="fk-caret"></span></span></div>
      <div class="fk-hbox">
        {% if show_mrp_discount %}<div class="fk-r fk-gray"><span>MRP Discount</span><span class="fk-green">&minus;&#8377;{{ mrp_discount_fmt }}</span></div>{% endif %}
        {% if coupon_disc > 0 %}<div class="fk-r fk-gray"><span>Coupons for you</span><span class="fk-green">&minus;&#8377;{{ coupon_disc_fmt }}</span></div>{% endif %}
        {% if best_bank_disc > 0 %}<div class="fk-r fk-gray"><span>Bank Offer Discount</span><span class="fk-green">&minus;&#8377;{{ best_bank_disc_fmt }}</span></div>{% endif %}
        <div class="fk-div"></div>
        <div class="fk-r fk-blue"><span>Total Amount <span class="fk-caret fk-caret-blue"></span></span><span class="fk-bold-blue">&#8377;{{ effective_fmt }}</span></div>
      </div>
      {% else %}
      <div class="fk-r"><span style="font-weight:600">Selling Price</span><span style="font-weight:700;font-size:18px;color:#2874f0">&#8377;{{ effective_fmt }}</span></div>
      {% endif %}
    </div>
  </div>
</div>
</body>
</html>"""
)


# ────────────────────────────────────────────────────────────────────
# 8. IMAGE GENERATION
# ────────────────────────────────────────────────────────────────────
def _download_image_b64(url):
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        img_bytes = r.content
        img = PILImage.open(BytesIO(img_bytes))
        w, h = img.size
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        return b64, w, h
    except Exception:
        return ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/58BAwAI/AL+hc2rNAAAAABJRU5ErkJggg==", 1, 1)

def _fmt(n):
    return f"{int(n):,}" if n else "0"

def generate_deal_image(image_url, bd, bank_offers, marketplace="amazon", template_type="standard", short_title="", reg_price=0):
    img_b64, orig_w, orig_h = _download_image_b64(image_url)

    if template_type == "optimized":
        effective = bd["effective"]
        show_reg = reg_price >= (bd["price"] * 1.20)
        
        if show_reg:
            reg_pct = int(((reg_price - effective) / reg_price) * 100)
            tag_pct = reg_pct
        else:
            mrp = bd.get("mrp", 0)
            tag_pct = int(((mrp - effective) / mrp) * 100) if mrp > effective else 0

        savings =[]
        if bd.get("coupon_disc", 0) > 0:
            savings.append({"label": "Coupon Savings", "value": _fmt(bd["coupon_disc"])})
        if bd.get("best_bank_disc", 0) > 0:
            savings.append({"label": f"{bd['best_bank']} Discount", "value": _fmt(bd["best_bank_disc"])})

        mkt_name = "AMAZON" if marketplace == "amazon" else "FLIPKART 🛒"
        theme_color = "#267383" if marketplace == "amazon" else "#2874f0"
        tag_color = "#f08800" if marketplace == "amazon" else "#f08800"
        
        html = OPTIMIZED_DEAL_TEMPLATE.render(
            img_b64=img_b64,
            marketplace_name=mkt_name,
            theme_color=theme_color,
            tag_color=tag_color,
            bg_color="#fcd528",
            product_title=short_title or "Product Deal",
            show_regular_price=show_reg,
            regular_price_fmt=_fmt(reg_price),
            regular_discount_pct=reg_pct if show_reg else 0,
            discount_pct=tag_pct,
            price_fmt=_fmt(bd["price"]),
            savings=savings,
            effective_fmt=_fmt(effective)
        )

    else:
        # Standard Layout Rendering
        aspect = orig_w / orig_h if orig_h > 0 else 1
        is_landscape = aspect > 1.3
        layout = "stack" if is_landscape else "side"
        canvas_width = 750 if is_landscape else 800
        img_max = 500 if is_landscape else 350
        pad = 28

        tpl = dict(
            layout=layout, canvas_width=canvas_width, img_max=img_max, pad=pad, img_b64=img_b64,
            price_fmt=_fmt(bd["price"]), coupon_disc=bd["coupon_disc"], coupon_disc_fmt=_fmt(bd["coupon_disc"]),
            effective_fmt=_fmt(bd["effective"]), best_bank=bd.get("best_bank") or "Bank",
            best_bank_disc=bd.get("best_bank_disc", 0), best_bank_disc_fmt=_fmt(bd.get("best_bank_disc", 0)),
        )

        if marketplace == "flipkart":
            mrp_discount = max(0, bd["mrp"] - bd["price"])
            has_any_discount = (mrp_discount > 0 or bd["coupon_disc"] > 0 or bd.get("best_bank_disc", 0) > 0)
            tpl.update(mrp_fmt=_fmt(bd["mrp"]), mrp_discount=mrp_discount, mrp_discount_fmt=_fmt(mrp_discount), show_mrp_discount=mrp_discount > 0, has_any_discount=has_any_discount)
            html = FLIPKART_DEAL_TEMPLATE.render(**tpl)
        else:
            savings_count = 0
            total_savings = 0
            if bd["coupon_disc"] > 0: savings_count += 1; total_savings += bd["coupon_disc"]
            if bd.get("best_bank_disc", 0) > 0: savings_count += 1; total_savings += bd["best_bank_disc"]
            tpl.update(savings_count=savings_count, total_savings_fmt=_fmt(total_savings))
            html = AMAZON_DEAL_TEMPLATE.render(**tpl)

    try:
        # 1. WeasyPrint generates a perfect PDF in memory
        pdf_bytes = HTML(string=html).write_pdf()
    
        # 2. PyMuPDF opens the PDF bytes and converts the first page to a PNG
        pdf_document = fitz.open("pdf", pdf_bytes)
        page = pdf_document.load_page(0)
        pix = page.get_pixmap(dpi=150) 
        png_bytes = pix.tobytes("png")
    
        # 3. Load the PNG into Pillow
        buf_in = BytesIO(png_bytes)
        img = PILImage.open(buf_in).convert("RGB")
        w, h = img.size

        # 4. Ultra-fast, artifact-proof threshold cropping
        gray = img.convert("L")
        bw = gray.point(lambda x: 0 if x > 250 else 255, '1')
        bbox = bw.getbbox()

        if bbox:
            img = img.crop((0, 0, w, min(bbox[3] + 15, h)))
    
        # 5. Save the cropped image
        buf_out = BytesIO()
        img.save(buf_out, format="PNG", quality=95)
        buf_out.seek(0)
        return buf_out

    except Exception as e:
        log.error(f"Render error: {e}")
        return None


# ────────────────────────────────────────────────────────────────────
# 9. CAPTION & TELEGRAM HANDLERS
# ────────────────────────────────────────────────────────────────────
def format_caption(title, url, bd, avg_price):
    effective = bd["effective"]
    has_savings = bd["coupon_disc"] > 0 or bd.get("best_bank_disc", 0) > 0
    header = f"{title} for ₹{effective:,} (<b>Effectively</b>)" if has_savings else f"{title} for ₹{bd['price']:,}"
    parts = []
    if bd["coupon_disc"] > 0: parts.append(f"₹{bd['coupon_disc']:,} off coupon")
    if bd.get("best_bank_disc", 0) > 0:
        bank_str = bd["best_bank"] + (" EMI" if bd.get("best_bank_is_emi") else "")
        parts.append(f"₹{bd['best_bank_disc']:,} off with {bank_str}")
    lines = [header, ""]
    if parts: lines.append(f"<b>📌Apply {' + '.join(parts)}</b>"); lines.append("")
    lines.append(url)
    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me any Amazon or Flipkart link.\nI'll generate a deal post with price breakdown & offers!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text or msg.caption or ""
    url_m = re.search(r"(https?://[^\s]+)", text)
    if not url_m: return
    raw_url = url_m.group(1)
    if not any(k in raw_url for k in ["amazon", "amzn", "flipkart", "fkrt"]): return

    status = await msg.reply_text("⏳ Processing...")

    try:
        resolved = resolve_url(raw_url)
        mkt, pid, pos = detect_marketplace(resolved)
        if not mkt or not pid:
            await status.edit_text("❌ Couldn't detect product.")
            return

        product_url = make_clean_url(mkt, pid, resolved)
        await status.edit_text("📦 Fetching data...")

        # Phase 1: parallel API calls (including the New Historical API)
        details, thunder, compare, reg_price = await asyncio.gather(
            api_product_details(resolved),
            api_thunder(pid, pos),
            api_compare(pid, pos),
            get_historical_regular_price(pid, pos),
            return_exceptions=True,
        )
        if isinstance(details, Exception): details = {}
        if isinstance(thunder, Exception): thunder = {}
        if isinstance(compare, Exception): compare =[]
        if isinstance(reg_price, Exception): reg_price = 0

        raw_title = details.get("prod") or details.get("title") or "Product"
        await status.edit_text("🔍 Scraping & preparing...")

        # Phase 2: parallel scrape + title shorten
        scrape_fn = scrape_amazon if mkt == "amazon" else scrape_flipkart
        scraped_result, short_title = await asyncio.gather(
            asyncio.to_thread(scrape_fn, product_url),
            shorten_title_groq(raw_title),
            return_exceptions=True,
        )

        if isinstance(scraped_result, Exception):
            scraped_result = {"current_price": None, "mrp": None, "coupon": None, "bank_offers":[]}
        if isinstance(short_title, Exception):
            short_title = raw_title

        scraped = scraped_result
        image_url = details.get("image", "")
        price = scraped.get("current_price") or details.get("price") or 0
        if not price and thunder.get("avg"): price = int(thunder["avg"])
        mrp = scraped.get("mrp") or details.get("mrp") or price
        if mrp < price: mrp = price
        avg_p = thunder.get("avg", 0)

        bd = calc_breakdown(price, mrp, scraped.get("coupon"), scraped.get("bank_offers",[]))
        await status.edit_text("🎨 Generating deal card...")

        # Save to Cache so we can instantly switch templates on button click without re-scraping!
        caption = format_caption(short_title, product_url, bd, avg_p)
        context.user_data['deal_cache'] = {
            'image_url': image_url, 'bd': bd, 'bank_offers': scraped.get("bank_offers",[]),
            'mkt': mkt, 'short_title': short_title, 'reg_price': reg_price, 'caption': caption
        }

        # Generate Standard Image first
        deal_img = await asyncio.to_thread(
            generate_deal_image, image_url, bd, scraped.get("bank_offers",[]), 
            marketplace=mkt, template_type="standard", short_title=short_title, reg_price=reg_price
        )

        # Build Interactive Button
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Show Optimized Version", callback_data="opt_version")]])

        if deal_img:
            await msg.reply_photo(photo=deal_img, caption=caption, parse_mode="HTML", reply_markup=keyboard)
        else:
            await msg.reply_text(caption, disable_web_page_preview=True, parse_mode="HTML")

        await status.delete()

    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
        await status.edit_text(f"❌ Error: {str(e)[:100]}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the Toggle Button to switch between Standard and Optimized cards instantly."""
    query = update.callback_query
    await query.answer("🎨 Generating new layout...", show_alert=False)
    
    data = query.data
    cache = context.user_data.get('deal_cache')
    
    if not cache:
        await query.edit_message_caption(caption="⏳ Session expired. Please send the link again.")
        return

    is_optimized = (data == "opt_version")
    new_text = "🖼️ Show Standard Version" if is_optimized else "🌟 Show Optimized Version"
    new_data = "std_version" if is_optimized else "opt_version"
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(new_text, callback_data=new_data)]])
    
    deal_img = await asyncio.to_thread(
        generate_deal_image, cache['image_url'], cache['bd'], cache['bank_offers'], 
        marketplace=cache['mkt'], template_type="optimized" if is_optimized else "standard", 
        short_title=cache['short_title'], reg_price=cache['reg_price']
    )
    
    if deal_img:
        await query.edit_message_media(
            media=InputMediaPhoto(deal_img, caption=cache['caption'], parse_mode="HTML"),
            reply_markup=keyboard
        )


def main():
    if BOT_TOKEN == "YOUR_TOKEN": raise ValueError("Set TELEGRAM_BOT_TOKEN environment variable!")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    keep_alive()
    log.info("DealBot v7 running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
