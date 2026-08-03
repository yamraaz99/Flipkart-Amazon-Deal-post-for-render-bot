#!/usr/bin/env python3
"""
Deal Post Bot v7.1 — Multi-Template Edition (Standard & Optimized)
Timeout-hardened build for low-CPU hosts (Render free tier).
"""

import os
import re
import json
import logging
import asyncio
import base64
import datetime
import random
from io import BytesIO
from urllib.parse import urlparse

import time
import telegram.error
import fitz
from weasyprint import HTML
import httpx
import requests
from bs4 import BeautifulSoup
from keep_alive import keep_alive
from fake_useragent import UserAgent
from PIL import Image as PILImage, ImageDraw, ImageFont
from jinja2 import Template

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.request import HTTPXRequest
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

SHORT_DOMAINS = [
    "amzn.to", "amzn.in", "bit.ly", "fkrt.site", "fkrt.cc",
    "fkrt.co", "fkrt.to", "dl.flipkart.com",
]

_BANK_RE = re.compile(
    r"((?:SBI|HDFC|ICICI|Axis|Kotak|RBL|HSBC|Yes\s*Bank|IndusInd|Federal|"
    r"BOB|Citi|AMEX|Amazon\s*Pay|OneCard|AU|Flipkart\s*Axis|BOBCARD)"
    r"(?:\s*Bank)?\s*(?:Credit|Debit)?\s*Card[s]?)",
    re.I,
)

BANK_COLORS = {
    "sbi": "#0d6efd", "hdfc": "#004b8d", "icici": "#f37920", "axis": "#97144d",
    "kotak": "#ed1c24", "rbl": "#21409a", "hsbc": "#db0011", "yes bank": "#0066b3",
    "indusind": "#8b1a4a", "federal": "#f7a800", "bob": "#f47920", "citi": "#003ea4",
    "amex": "#006fcf", "amazon pay": "#ff9900", "onecard": "#000000", "au": "#ec1c24",
    "flipkart axis": "#2874f0", "bobcard": "#f47920",
}


def _get_bank_color(bank_name):
    name = bank_name.lower()
    for key, color in BANK_COLORS.items():
        if key in name:
            return color
    return "#666666"


# ─────────────────────────────────────────────
# 1. URL HANDLING
# ─────────────────────────────────────────────
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


# ─────────────────────────────────────────────
# 2. HEADERS
# ─────────────────────────────────────────────
# FIX: build UserAgent ONCE (it was re-initialising on every single request)
try:
    _UA = UserAgent(
        fallback="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/126.0.0.0 Safari/537.36"
    )
except Exception:
    _UA = None

_UA_FALLBACK = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36")


def _desktop_headers():
    try:
        ua = _UA.random if _UA else _UA_FALLBACK
    except Exception:
        ua = _UA_FALLBACK
    return {
        "User-Agent": ua,
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


# ─────────────────────────────────────────────
# 3. BUYHATKE & HISTORICAL APIs
# ─────────────────────────────────────────────
async def api_product_details(url):
    try:
        async with httpx.AsyncClient(timeout=12) as c:
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
            return r.json().get("data", [])
    except Exception as e:
        log.error(f"api_compare: {e}")
        return []


async def get_historical_regular_price(pid, pos):
    """Fetches historical price data to establish a 'Regular Price'"""
    url = (f"https://graph.bitbns.com/getPredictedData.php?type=log"
           f"&indexName=interest_centers&logName=info&pos={pos}&pid={pid}&mainFL=1")
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(url)
            if r.status_code == 200 and "*" in r.text:
                parts = r.text.split("~*")
                prices = []
                for p in parts:
                    if "~" in p:
                        try:
                            price_str = p.split("~")[1].split("&")[0].strip()
                            val = int(price_str)
                            if val > 0:
                                prices.append(val)
                        except Exception:
                            pass
                if prices:
                    return sum(prices) // len(prices)
    except Exception as e:
        log.error(f"Error fetching regular price: {e}")
    return 0


async def api_product_data(pid, pos):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://buyhatke.com/api/productData?pos={pos}&pid={pid}")
            d = r.json()
            return d.get("data", {}) if d.get("status") == 1 else {}
    except Exception as e:
        log.error(f"api_product_data: {e}")
        return {}


# ─────────────────────────────────────────────
# 4. BANK OFFER EXTRACTION & SCRAPERS
# ─────────────────────────────────────────────
def _extract_bank_offers_amazon(soup):
    offers = []
    seen = set()

    for card in soup.select(
        '#poExpander .a-carousel-card, #ppd .a-carousel-card, .a-carousel-card, '
        '[data-feature-name="buyNowFitWidget"] .a-box, '
        '[data-feature-name="buyNowFit498Widget"] .a-box'
    ):
        text = card.get_text(" ", strip=True)
        buy_match = re.search(r"Buy\s+for\s*(?:₹|Rs\.?)\s*([\d,]+)", text, re.I)
        if not buy_match:
            continue
        final_price = int(buy_match.group(1).replace(",", ""))
        coupon_match = re.search(r"Coupon\s*[-−]?\s*(?:₹|Rs\.?)\s*([\d,]+)", text, re.I)
        coupon_amt = int(coupon_match.group(1).replace(",", "")) if coupon_match else 0
        bank_match = _BANK_RE.search(text)
        if not bank_match:
            continue
        bank_name = bank_match.group(1).strip()
        if bank_name.lower() in seen:
            continue
        seen.add(bank_name.lower())
        bank_disc_match = re.search(
            re.escape(bank_name) + r".*?[-−]\s*(?:₹|Rs\.?)\s*([\d,]+)", text, re.I
        )
        bank_disc = int(bank_disc_match.group(1).replace(",", "")) if bank_disc_match else 0
        is_emi = bool(re.search(r"\bEMI\b", text, re.I))
        offers.append({
            "bank": bank_name, "discount_flat": bank_disc,
            "coupon_in_card": coupon_amt, "final_price": final_price,
            "is_emi": is_emi, "text": text[:150],
        })

    selectors = (
        '#poExpander li, #soWidget li, #itembox-InstallmentCalculator li, '
        '[data-csa-c-content-id*="offer"] li, .a-unordered-list .a-list-item'
    )
    for item in soup.select(selectors):
        txt = item.get_text(" ", strip=True)
        if len(txt) < 15 or len(txt) > 400:
            continue
        bm = _BANK_RE.search(txt)
        if not bm:
            continue
        bank = bm.group(1).strip()
        if bank.lower() in seen:
            continue
        seen.add(bank.lower())
        offer = {"bank": bank, "text": txt[:150], "is_emi": False}
        pct = re.search(r"(\d+)\s*%\s*(?:instant\s*)?(?:discount|off|cashback|savings)", txt, re.I)
        flat = re.search(r"(?:₹|Rs\.?|INR)\s*([\d,]+)\s*(?:instant\s*)?(?:discount|off|cashback|savings)", txt, re.I)
        cap = re.search(r"(?:up\s*to|upto|max\.?)\s*(?:₹|Rs\.?|INR)\s*([\d,]+)", txt, re.I)
        if pct:
            offer["discount_pct"] = int(pct.group(1))
        if flat:
            offer["discount_flat"] = int(flat.group(1).replace(",", ""))
        if cap:
            offer["max_discount"] = int(cap.group(1).replace(",", ""))
        if re.search(r"\bEMI\b", txt, re.I):
            offer["is_emi"] = True
        offers.append(offer)

    return offers


def _extract_flipkart_bank_offers_json(html_text):
    pattern = re.compile(r'\{"type":"NepOffers","bankCardType":"BANK_OFFER_PILL"')
    offers = []
    seen = set()
    for match in pattern.finditer(html_text):
        fragment = html_text[match.start():]
        depth, end_idx = 0, -1
        for i, ch in enumerate(fragment[:10000]):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
        if end_idx == -1:
            continue
        try:
            obj = json.loads(fragment[: end_idx + 1])
        except (json.JSONDecodeError, ValueError):
            continue
        bank = obj.get("offerTitle", "").strip()
        discount_text = obj.get("discountedPriceText", "").strip()
        if not bank or not discount_text:
            continue
        card_type = ""
        try:
            content_list = obj["offerSubTitleRC"]["value"]["contentList"]
            card_type = " • ".join(
                x["contentValue"] for x in content_list if x.get("contentType") == "TEXT"
            )
        except (KeyError, TypeError):
            pass
        card_type_clean = card_type.split("•")[0].strip() if card_type else ""
        full_bank = f"{bank} {card_type_clean}".strip() if card_type_clean else bank
        dedup_key = full_bank.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        disc_match = re.search(r"[\d,]+", discount_text.replace("₹", ""))
        disc_amt = int(disc_match.group().replace(",", "")) if disc_match else 0
        if disc_amt <= 0:
            continue
        is_emi = bool(re.search(r"\bemi\b", card_type, re.I))
        offers.append({
            "bank": full_bank, "discount_flat": disc_amt, "is_emi": is_emi,
            "text": f"{discount_text} {bank} {card_type}"[:150],
        })
    offers.sort(key=lambda x: x.get("discount_flat", 0), reverse=True)
    return offers


def scrape_amazon(url):
    result = {"current_price": None, "mrp": None, "coupon": None, "bank_offers": []}
    try:
        s = requests.Session()
        s.headers.update(_desktop_headers())
        resp = s.get(url, timeout=8)
        soup = BeautifulSoup(resp.content, "html.parser")
        if "captcha" not in resp.text.lower()[:2000]:
            for sel in [".priceToPay .a-price-whole", ".a-price .a-offscreen",
                        "#priceblock_ourprice", "#priceblock_dealprice",
                        "#corePriceDisplay_desktop_feature_div .a-price-whole",
                        "span.a-price-whole"]:
                el = soup.select_one(sel)
                if el:
                    p = _clean_price(el.get_text())
                    if p and p > 0:
                        result["current_price"] = p
                        break
            for sel in [".a-price.a-text-price .a-offscreen", ".basisPrice .a-offscreen",
                        "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen"]:
                el = soup.select_one(sel)
                if el:
                    m = _clean_price(el.get_text())
                    if m and m > 0:
                        result["mrp"] = m
                        break
            if not result["mrp"]:
                result["mrp"] = result["current_price"]

            for sel in ["#coupons-card-sub-heading-before-apply", 'label[id^="couponText"]',
                        ".promoPriceBlockMessage", "#couponBadgeRegularVpc"]:
                el = soup.select_one(sel)
                if el:
                    txt = el.get_text(strip=True)
                    if any(w in txt.lower() for w in ["coupon", "save", "%", "₹"]):
                        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", txt)
                        flat = re.search(r"(?:₹|Rs\.?)\s*(\d[\d,]*)", txt, re.I)
                        if pct:
                            result["coupon"] = {"type": "percent", "value": float(pct.group(1)), "text": txt}
                        elif flat:
                            result["coupon"] = {"type": "flat", "value": int(flat.group(1).replace(",", "")), "text": txt}
                        break
            if not result["coupon"]:
                for lbl in soup.find_all("label"):
                    t = lbl.get_text(strip=True)
                    if "coupon" in t.lower() and ("apply" in t.lower() or "save" in t.lower()):
                        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
                        flat = re.search(r"(?:₹|Rs\.?)\s*(\d[\d,]*)", t, re.I)
                        if pct:
                            result["coupon"] = {"type": "percent", "value": float(pct.group(1)), "text": t}
                        elif flat:
                            result["coupon"] = {"type": "flat", "value": int(flat.group(1).replace(",", "")), "text": t}
                        break

            result["bank_offers"] = _extract_bank_offers_amazon(soup)
    except Exception:
        pass

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
                    if o["bank"].lower() not in existing:
                        result["bank_offers"].append(o)
                if not result["current_price"]:
                    for sel in [".a-price .a-offscreen", "#newPrice .a-offscreen",
                                'span[data-a-color="price"] .a-offscreen']:
                        el = soup2.select_one(sel)
                        if el:
                            p = _clean_price(el.get_text())
                            if p and p > 0:
                                result["current_price"] = p
                                break
        except Exception:
            pass
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
            except Exception:
                pass
            sess.close()
        except Exception:
            pass
    try:
        s = requests.Session()
        s.headers.update(_desktop_headers())
        resp = s.get(url, timeout=8)
        if resp.status_code == 200 and len(resp.text) > 5000:
            return resp.text
    except Exception:
        pass
    return ""


def scrape_flipkart(url):
    result = {"current_price": None, "mrp": None, "coupon": None, "bank_offers": []}
    html_text = _fetch_flipkart_html(url)
    if not html_text:
        return result
    soup = BeautifulSoup(html_text, "html.parser")

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text)
            if isinstance(data, list):
                data = data[0]
            if data.get("@type") == "Product":
                offers = data.get("offers", {})
                if isinstance(offers, list) and offers:
                    result["current_price"] = _clean_price(str(offers[0].get("price")))
                elif isinstance(offers, dict):
                    result["current_price"] = _clean_price(str(offers.get("price")))
        except Exception:
            continue

    if not result["current_price"]:
        for pat in [r'"sellingPrice"\s*:\s*(\d+)', r'"finalPrice"\s*:\s*(\d+)']:
            m = re.search(pat, html_text)
            if m:
                val = int(m.group(1))
                if val > 0:
                    result["current_price"] = val
                    break

    for sel in ["div.yRaY8j", "div._3I9_wc"]:
        el = soup.select_one(sel)
        if el:
            result["mrp"] = _clean_price(el.get_text())
            break
    if not result["mrp"]:
        for pat in [r'"mrp"\s*:\s*(\d+)', r'"maximumRetailPrice"\s*:\s*(\d+)']:
            m = re.search(pat, html_text)
            if m:
                val = int(m.group(1))
                if val > 0:
                    result["mrp"] = val
                    break
    if not result["mrp"]:
        result["mrp"] = result["current_price"]

    result["bank_offers"] = _extract_flipkart_bank_offers_json(html_text)
    return result


async def shorten_title_groq(full_title):
    if not GROQ_API_KEY:
        return full_title
    if len(full_title) <= 70:
        return full_title
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [
                        {"role": "system", "content":
                            "You shorten e-commerce product titles. Keep: brand, key specs "
                            "(size, capacity, star rating, color), product type. Remove: model "
                            "codes, marketing buzzwords, AI features, pipe-separated feature "
                            "lists, processor names. Max ~80 characters. Return ONLY the title, "
                            "nothing else."},
                        {"role": "user", "content": full_title},
                    ],
                    "temperature": 0,
                    "max_tokens": 100,
                },
            )
            data = resp.json()
            shortened = data["choices"][0]["message"]["content"].strip().strip('"').strip("'")
            if shortened and len(shortened) > 10:
                return shortened
    except Exception:
        pass
    return full_title


def calc_breakdown(price, mrp, coupon, bank_offers):
    b = {
        "mrp": mrp or price or 0, "price": price or 0, "coupon_disc": 0,
        "coupon_text": None, "after_coupon": price or 0, "best_bank": None,
        "best_bank_disc": 0, "best_bank_is_emi": False, "effective": price or 0,
        "coupon_type": None, "coupon_raw_value": 0,
    }
    if not price:
        return b
    if coupon:
        b["coupon_type"] = coupon["type"]
        b["coupon_raw_value"] = coupon["value"]
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
            if d < 0:
                d = 0
        elif "discount_flat" in o:
            d = o["discount_flat"]
        elif "discount_pct" in o:
            d = int(ap * o["discount_pct"] / 100)
            if "max_discount" in o:
                d = min(d, o["max_discount"])
        if d > b["best_bank_disc"]:
            b["best_bank_disc"] = d
            b["best_bank"] = o["bank"]
            b["best_bank_is_emi"] = o.get("is_emi", False)

    b["effective"] = ap - b["best_bank_disc"]
    return b


# ─────────────────────────────────────────────
# 7. HTML TEMPLATES  (⚠️ UNCHANGED — PASTE YOUR EXISTING STRINGS HERE)
# ─────────────────────────────────────────────
# Your PDF export rendered the HTML instead of preserving the source, so the CSS
# is not recoverable. None of the timeout fixes touch these three blocks —
# just paste your originals verbatim.

OPTIMIZED_DEAL_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
    @page { size: 900px 420px; margin: 0; }
    body { background-color: #f7f9fa; font-family: Arial, sans-serif; margin: 0; padding: 35px; -webkit-font-smoothing: antialiased; }
    .product-card { display: table; width: 830px; background-color: transparent; color: #0f1111; }
    .image-col { display: table-cell; vertical-align: middle; width: 250px; padding-right: 35px; text-align: center; }
    .image-wrapper { display: inline-block; line-height: 0; }
    .image-wrapper img { max-width: 240px; max-height: 240px; object-fit: contain; }
    .details-col { display: table-cell; vertical-align: middle; width: 580px; }
    .product-title { font-size: 28px; font-weight: 400; margin: 0 0 12px 0; line-height: 1.35; color: #0f1111; }
    .bought-stats { font-size: 24px; color: #0f1111; margin: 0 0 16px 0; }
    .deal-tag { color: #cc0c39; font-size: 24px; font-weight: 700; margin: 0 0 15px 0; }
    .pricing-row { margin-bottom: 8px; }
    .discount-box { display: inline-block; background-color: #cc0c39; color: #ffffff; padding: 8px 14px; border-radius: 6px; font-size: 32px; font-weight: 400; vertical-align: middle; margin-right: 15px; }
    .price-block { display: inline-block; vertical-align: middle; }
    .currency-sym { display: inline-block; font-size: 24px; font-weight: 500; vertical-align: top; margin-top: 6px; margin-right: 2px; }
    .price-main { display: inline-block; font-size: 52px; font-weight: 700; line-height: 1; vertical-align: middle; letter-spacing: -1px; }
    .price-cents { display: inline-block; font-size: 20px; font-weight: 700; vertical-align: top; margin-top: 4px; }
    .mrp-row { font-size: 24px; color: #565959; margin-bottom: 12px; }
    .mrp-strike { text-decoration: line-through; }
    .prime-row { margin-bottom: 12px; }
    .prime-logo-wrapper { display: inline-block; vertical-align: middle; }
    .prime-tick { display: inline-block; vertical-align: middle; margin-right: 2px; }
    .prime-text { display: inline-block; vertical-align: middle; color: #00a8e1; font-weight: 700; font-size: 26px; letter-spacing: -0.5px; }
    .today-badge { display: inline-block; vertical-align: middle; background-color: #1ea0f5; color: #ffffff; font-size: 22px; font-weight: 700; font-style: italic; padding: 3px 10px; border-radius: 4px; margin-left: 10px; }
    .delivery-info { font-size: 22px; color: #0f1111; }
    .delivery-info strong { font-weight: 700; }
</style>
</head>
<body>
    <div class="product-card">
        <div class="image-col">
            <div class="image-wrapper"><img src="data:image/jpeg;base64,{{ img_b64 }}" alt="Product Image"></div>
        </div>
        <div class="details-col">
            <h2 class="product-title">{{ title }}</h2>
            <div class="bought-stats">{{ bought_stats }}</div>
            <div class="deal-tag">Limited time deal</div>
            <div class="pricing-row">
                {% if percent_off %}
                <div class="discount-box">{{ percent_off }}</div>
                {% endif %}
                <div class="price-block">
                    <span class="currency-sym">₹</span><span class="price-main">{{ current_price }}</span><span class="price-cents">{{ price_cents }}</span>
                </div>
            </div>
            {% if mrp %}
            <div class="mrp-row">M.R.P.: <span class="mrp-strike">₹{{ mrp }}</span></div>
            {% endif %}
            <div class="prime-row">
                <div class="prime-logo-wrapper">
                    <svg class="prime-tick" width="22" height="16" viewBox="0 0 24 18" fill="none" stroke="#FF9900" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 10 9 15 20 2"></polyline></svg>
                    <span class="prime-text">prime</span>
                </div>
                <div class="today-badge">Tomorrow</div>
            </div>
            <div class="delivery-info">{{ delivery_prefix }} <strong>{{ delivery_date }}</strong></div>
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
        <div class="cpn-desc"><span class="cpn-green">Save {{ coupon_display_text }}</span> with coupon</div>
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


# ─────────────────────────────────────────────
# 8. IMAGE GENERATION
# ─────────────────────────────────────────────
_WM_STAMP = None  # cached rotated watermark stamp (built once per process)


def _get_watermark_stamp(text):
    """Builds the rotated watermark tile ONCE and caches it.
    Uses mode 'L' (alpha mask only) instead of RGBA — same pixels, 4x cheaper."""
    global _WM_STAMP
    if _WM_STAMP is None:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        font_path = os.path.join(current_dir, "Roboto-Bold.ttf")
        try:
            font = ImageFont.truetype(font_path, 50)
        except Exception as e:
            log.error(f"Font error: {e}. Path checked: {font_path}")
            font = ImageFont.load_default()

        stamp = PILImage.new("L", (600, 150), 0)
        stamp_draw = ImageDraw.Draw(stamp)
        stamp_draw.text((50, 50), text, fill=115, font=font)   # 115 = same opacity as before
        _WM_STAMP = stamp.rotate(30, expand=1, resample=PILImage.BICUBIC)
    return _WM_STAMP


def apply_repeating_watermark(img, text="AmazingDealsLoots"):
    """Identical output to the old RGBA/alpha_composite version, but:
       - no full-image RGBA conversion
       - no second full RGBA overlay
       - stamp/font built once, not per render"""
    stamp = _get_watermark_stamp(text)
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    sw, sh = stamp.size

    step_x = max(1, sw - 220)
    step_y = max(1, sh - 120)

    mask = PILImage.new("L", (w, h), 0)
    for y in range(-sh, h, step_y):
        offset = (y // step_y) % 2 * (step_x // 2)
        for x in range(-sw + offset, w, step_x):
            mask.paste(stamp, (x, y), stamp)

    img.paste(PILImage.new("RGB", (w, h), (0, 0, 0)), (0, 0), mask)
    return img


def _download_image_b64(url):
    """Downloads the product image and downscales it to a sane size before
    embedding. It's rendered into a <=500 CSS px slot (~781px @150dpi), so a
    2000px source is pure WeasyPrint CPU waste with zero visual gain."""
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        img_bytes = r.content
        img = PILImage.open(BytesIO(img_bytes))
        w, h = img.size

        if max(w, h) > 900:
            img = img.copy()
            img.thumbnail((900, 900), PILImage.LANCZOS)
            if img.mode in ("RGBA", "LA", "P"):
                bg = PILImage.new("RGB", img.size, (255, 255, 255))
                img_rgba = img.convert("RGBA")
                bg.paste(img_rgba, mask=img_rgba.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
            tmp = BytesIO()
            img.save(tmp, format="JPEG", quality=90, optimize=False)
            img_bytes = tmp.getvalue()

        b64 = base64.b64encode(img_bytes).decode("utf-8")
        return b64, w, h
    except Exception:
        return (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/58BAwAI/AL+"
            "hc2rNAAAAABJRU5ErkJggg==", 1, 1)


def _fmt(n):
    return f"{int(n):,}" if n else "0"


def generate_deal_image(image_url, bd, bank_offers, marketplace="amazon",
                        template_type="standard", short_title="", reg_price=0):
    img_b64, orig_w, orig_h = _download_image_b64(image_url)

    if template_type == "optimized":
        effective = bd["effective"]
        real_mrp = bd.get("mrp", effective)
        tag_pct = int(((real_mrp - effective) / real_mrp) * 100) if real_mrp > effective else 0

        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        del_date = tomorrow.strftime('%d %b')

        bought_rnd = random.choice(['100+', '200+', '400+', '500+', '1K+', '2K+', '3K+'])
        bought_stats = f"{bought_rnd} bought in past month"

        html = OPTIMIZED_DEAL_TEMPLATE.render(
            img_b64=img_b64,
            title=short_title or "Product Deal",
            bought_stats=bought_stats,
            percent_off=f"-{tag_pct}%" if tag_pct > 0 else "",
            current_price=_fmt(effective),
            price_cents="00",
            mrp=_fmt(real_mrp) if real_mrp > effective else "",
            delivery_prefix="FREE delivery",
            delivery_date=f"Tomorrow, {del_date}",
        )
    else:
        aspect = orig_w / orig_h if orig_h > 0 else 1
        is_landscape = aspect > 1.3
        layout = "stack" if is_landscape else "side"
        canvas_width = 750 if is_landscape else 800
        img_max = 500 if is_landscape else 350
        pad = 28

        tpl = dict(
            layout=layout, canvas_width=canvas_width, img_max=img_max, pad=pad,
            img_b64=img_b64,
            price_fmt=_fmt(bd["price"]),
            coupon_disc=bd["coupon_disc"],
            coupon_disc_fmt=_fmt(bd["coupon_disc"]),
            effective_fmt=_fmt(bd["effective"]),
            best_bank=bd.get("best_bank") or "Bank",
            best_bank_disc=bd.get("best_bank_disc", 0),
            best_bank_disc_fmt=_fmt(bd.get("best_bank_disc", 0)),
        )

        if marketplace == "flipkart":
            mrp_discount = max(0, bd["mrp"] - bd["price"])
            has_any_discount = (mrp_discount > 0 or bd["coupon_disc"] > 0
                                or bd.get("best_bank_disc", 0) > 0)
            tpl.update(
                mrp_fmt=_fmt(bd["mrp"]),
                mrp_discount=mrp_discount,
                mrp_discount_fmt=_fmt(mrp_discount),
                show_mrp_discount=mrp_discount > 0,
                has_any_discount=has_any_discount,
            )
            html = FLIPKART_DEAL_TEMPLATE.render(**tpl)
        else:
            savings_count = 0
            total_savings = 0
            if bd["coupon_disc"] > 0:
                savings_count += 1
                total_savings += bd["coupon_disc"]
            if bd.get("best_bank_disc", 0) > 0:
                savings_count += 1
                total_savings += bd["best_bank_disc"]

            if bd.get("coupon_type") == "percent":
                coupon_display_text = f"{bd['coupon_raw_value']:g}%"
            else:
                coupon_display_text = f"&#8377;{_fmt(bd['coupon_disc'])}"

            tpl.update(
                savings_count=savings_count,
                total_savings_fmt=_fmt(total_savings),
                coupon_display_text=coupon_display_text,
            )
            html = AMAZON_DEAL_TEMPLATE.render(**tpl)

    try:
        # 1. WeasyPrint -> PDF in memory
        pdf_bytes = HTML(string=html).write_pdf()

        # 2. PyMuPDF -> PNG pixmap
        pdf_document = fitz.open("pdf", pdf_bytes)
        page = pdf_document.load_page(0)
        pix = page.get_pixmap(dpi=150)
        png_bytes = pix.tobytes("png")
        pdf_document.close()

        # 3. Pillow
        img = PILImage.open(BytesIO(png_bytes)).convert("RGB")
        w, h = img.size

        # 4. Threshold crop
        gray = img.convert("L")
        bw = gray.point(lambda x: 0 if x > 250 else 255, '1')
        bbox = bw.getbbox()
        if bbox:
            img = img.crop((0, 0, w, min(bbox[3] + 15, h)))

        # 5. Watermark (cached stamp + mask paste)
        img = apply_repeating_watermark(img, text="AmazingDealsLoots")

        # 6. Save as JPEG.
        #    Telegram re-encodes every photo to JPEG anyway, so a watermarked PNG
        #    (1.5–3 MB, because tiled AA text kills PNG compression) was being
        #    uploaded for nothing. q92 + subsampling=0 keeps text razor sharp.
        buf_out = BytesIO()
        img.save(buf_out, format="JPEG", quality=92, subsampling=0, optimize=False)
        buf_out.name = "deal.jpg"
        buf_out.seek(0)
        return buf_out

    except Exception as e:
        log.error(f"Render error: {e}")
        return None


# ─────────────────────────────────────────────
# 9. CAPTION & TELEGRAM HANDLERS
# ─────────────────────────────────────────────
def format_caption(title, url, bd, avg_price):
    effective = bd["effective"]
    has_savings = bd["coupon_disc"] > 0 or bd.get("best_bank_disc", 0) > 0
    header = (f"{title} for ₹{effective:,} (Effectively)"
              if has_savings else f"{title} for ₹{bd['price']:,}")
    parts = []

    if bd["coupon_disc"] > 0:
        if bd.get("coupon_type") == "percent":
            parts.append(f"{bd['coupon_raw_value']:g}% off coupon")
        else:
            parts.append(f"₹{bd['coupon_disc']:,} off coupon")

    if bd.get("best_bank_disc", 0) > 0:
        bank_str = bd["best_bank"] + (" EMI" if bd.get("best_bank_is_emi") else "")
        parts.append(f"₹{bd['best_bank_disc']:,} off with {bank_str}")

    lines = [header, ""]
    if parts:
        lines.append(f"<b>Apply {' + '.join(parts)}</b>")
        lines.append("")
    lines.append(url)
    return "\n".join(lines)


async def _safe(coro):
    """Fire a Telegram call, swallow network hiccups (so a failed status
    edit/delete never masquerades as a processing error)."""
    try:
        return await coro
    except Exception as e:
        log.warning(f"Telegram call ignored: {e}")
        return None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me any Amazon or Flipkart link.\n"
        "I'll generate a deal post with price breakdown & offers!"
    )


async def cmd_optimized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggles the default template between Standard and Optimized."""
    current_mode = context.user_data.get('default_mode', 'standard')
    if current_mode == 'standard':
        context.user_data['default_mode'] = 'optimized'
        await update.message.reply_text(
            "Default mode set to *Optimized*.\nAll future links will generate the "
            "optimized post first. You can still use the inline button to switch.",
            parse_mode="Markdown",
        )
    else:
        context.user_data['default_mode'] = 'standard'
        await update.message.reply_text(
            "Default mode set to *Standard*.\nAll future links will generate the "
            "standard post first. You can still use the inline button to switch.",
            parse_mode="Markdown",
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    text = msg.text or msg.caption or ""
    url_m = re.search(r"(https?://[^\s]+)", text)
    if not url_m:
        return
    raw_url = url_m.group(1)
    if not any(k in raw_url for k in ["amazon", "amzn", "flipkart", "fkrt"]):
        return

    status = await msg.reply_text("⏳ Processing...")

    try:
        # FIX: was blocking the event loop for up to 10s
        resolved = await asyncio.to_thread(resolve_url, raw_url)
        mkt, pid, pos = detect_marketplace(resolved)
        if not mkt or not pid:
            await _safe(status.edit_text("❌ Couldn't detect product."))
            return

        product_url = make_clean_url(mkt, pid, resolved)

        # Phase 1: parallel API calls
        details, thunder, compare, reg_price, prod_data = await asyncio.gather(
            api_product_details(resolved),
            api_thunder(pid, pos),
            api_compare(pid, pos),
            get_historical_regular_price(pid, pos),
            api_product_data(pid, pos),
            return_exceptions=True,
        )
        if isinstance(details, Exception): details = {}
        if isinstance(thunder, Exception): thunder = {}
        if isinstance(compare, Exception): compare = []
        if isinstance(reg_price, Exception): reg_price = 0
        if isinstance(prod_data, Exception): prod_data = {}

        raw_title = (prod_data.get("name") or details.get("prod")
                     or details.get("title") or "Product")

        # Phase 2: parallel scrape + title shorten
        scrape_fn = scrape_amazon if mkt == "amazon" else scrape_flipkart
        scraped_result, short_title = await asyncio.gather(
            asyncio.to_thread(scrape_fn, product_url),
            shorten_title_groq(raw_title),
            return_exceptions=True,
        )
        if isinstance(scraped_result, Exception):
            scraped_result = {"current_price": None, "mrp": None,
                              "coupon": None, "bank_offers": []}
        if isinstance(short_title, Exception):
            short_title = raw_title

        scraped = scraped_result
        image_url = prod_data.get("image") or details.get("image") or ""

        price = (
            scraped.get("current_price")
            or _clean_price(prod_data.get("cur_price"))
            or _clean_price(details.get("price"))
            or 0
        )
        if not price and thunder.get("avg"):
            price = int(thunder["avg"])

        api_mrp = (
            _clean_price(prod_data.get("mrpFloat"))
            or _clean_price(details.get("mrp"))
            or _clean_price(details.get("mrpFloat"))
            or 0
        )
        scraped_mrp = _clean_price(scraped.get("mrp")) or 0
        mrp = max(scraped_mrp, api_mrp, price)

        avg_p = thunder.get("avg", 0)
        bd = calc_breakdown(price, mrp, scraped.get("coupon"), scraped.get("bank_offers", []))

        await _safe(status.edit_text("🎨 Generating deal card..."))

        caption = format_caption(short_title, product_url, bd, avg_p)
        context.user_data['deal_cache'] = {
            'image_url': image_url, 'bd': bd,
            'bank_offers': scraped.get("bank_offers", []),
            'mkt': mkt, 'short_title': short_title,
            'reg_price': reg_price, 'caption': caption,
        }

        default_mode = context.user_data.get('default_mode', 'standard')

        deal_img = await asyncio.to_thread(
            generate_deal_image, image_url, bd, scraped.get("bank_offers", []),
            marketplace=mkt, template_type=default_mode,
            short_title=short_title, reg_price=reg_price,
        )

        if default_mode == "optimized":
            btn_text, btn_cb = "🔄 Show Standard Version", "std_version"
        else:
            btn_text, btn_cb = "🔄 Show Optimized Version", "opt_version"

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, callback_data=btn_cb)]])

        if deal_img:
            await msg.reply_photo(
                photo=deal_img, caption=caption, parse_mode="HTML",
                reply_markup=keyboard,
                read_timeout=90, write_timeout=180, connect_timeout=30, pool_timeout=30,
            )
        else:
            await msg.reply_text(caption, disable_web_page_preview=True, parse_mode="HTML")

        await _safe(status.delete())

    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
        await _safe(status.edit_text(f"❌ Error: {str(e)[:100]}"))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle between Standard and Optimized cards instantly."""
    query = update.callback_query
    await _safe(query.answer("🎨 Generating new layout...", show_alert=False))

    data = query.data
    cache = context.user_data.get('deal_cache')
    if not cache:
        await _safe(query.edit_message_caption(
            caption="⚠️ Session expired. Please send the link again."))
        return

    is_optimized = (data == "opt_version")
    new_text = "🔄 Show Standard Version" if is_optimized else "🔄 Show Optimized Version"
    new_data = "std_version" if is_optimized else "opt_version"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(new_text, callback_data=new_data)]])

    deal_img = await asyncio.to_thread(
        generate_deal_image, cache['image_url'], cache['bd'], cache['bank_offers'],
        marketplace=cache['mkt'],
        template_type="optimized" if is_optimized else "standard",
        short_title=cache['short_title'], reg_price=cache['reg_price'],
    )

    if deal_img:
        await _safe(query.edit_message_media(
            media=InputMediaPhoto(deal_img, caption=cache['caption'], parse_mode="HTML"),
            reply_markup=keyboard,
            read_timeout=90, write_timeout=180, connect_timeout=30, pool_timeout=30,
        ))


def main():
    if BOT_TOKEN == "YOUR_TOKEN":
        raise ValueError("Set TELEGRAM_BOT_TOKEN environment variable!")

    # Start the keep-alive server ONCE before the loop, 
    # so Flask doesn't crash trying to use the same port twice.
    keep_alive()

    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            # Exactly your existing network settings
            request = HTTPXRequest(
                connection_pool_size=16,
                connect_timeout=30.0,
                read_timeout=60.0,
                write_timeout=120.0,
                pool_timeout=30.0,
            )
            get_updates_request = HTTPXRequest(
                connection_pool_size=4,
                connect_timeout=30.0,
                read_timeout=40.0,
                write_timeout=30.0,
                pool_timeout=30.0,
            )

            # Build the app instance
            app = (
                Application.builder()
                .token(BOT_TOKEN)
                .request(request)
                .get_updates_request(get_updates_request)
                .build()
            )

            # Exactly your existing handlers
            app.add_handler(CommandHandler("start", cmd_start))
            app.add_handler(CommandHandler("optimized", cmd_optimized))
            app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))
            app.add_handler(CallbackQueryHandler(handle_callback))

            log.info(f"DealBot v7.1 starting... (Attempt {attempt}/{max_retries})")

            # Start polling. (With drop_pending_updates=True just like you had)
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
            
            # If run_polling exits cleanly, exit the loop
            break

        except telegram.error.NetworkError as e:
            log.warning(f"Network error during startup (Attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                log.info("Waiting 5 seconds for Render's network to stabilize before rebuilding...")
                time.sleep(5) 
            else:
                log.error("Failed to connect to Telegram after 5 attempts. Aborting.")
                raise e
        except Exception as e:
            log.error(f"Unexpected startup error (Attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(5)
            else:
                raise e

if __name__ == "__main__":
    main()
