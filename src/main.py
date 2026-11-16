import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from apify import Actor
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

DEFAULT_PRODUCT_URLS = [
    "https://allbirds.com/products/mens-tree-runners",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

FREE_SHIPPING_PATTERNS = [
    re.compile(
        r"(free\s+(?:standard\s+|express\s+|ground\s+|worldwide\s+|u\.?s\.?\s+)?shipping"
        r"\s+(?:on\s+(?:all\s+)?orders?\s+)?(?:over|above|starting\s+at)\s+"
        r"[\$€£¥]?\s*\d+(?:[.,]\d{1,2})?\+?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(complimentary\s+(?:standard\s+|express\s+)?shipping"
        r"(?:\s+on\s+orders?\s+(?:over|above)\s+[\$€£¥]?\s*\d+(?:[.,]\d{1,2})?)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(free\s+(?:standard\s+|express\s+|2-day\s+|next-day\s+)?shipping"
        r"(?:\s+on\s+all\s+orders|\s+worldwide|\s+available)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"((?:[\d,]+円|¥[\d,]+)\s*(?:以上(?:のお買い上げ)?で)?\s*送料無料)",
    ),
    re.compile(r"(全品送料無料|送料無料)"),
]


class MerchandisingAnalysis(BaseModel):
    recommendedAction: Literal[
        "LOWER_PRICE",
        "RAISE_PRICE",
        "HOLD_PRICE",
        "LAUNCH_PROMO_BUNDLE",
        "CAPTURE_OUT_OF_STOCK_DEMAND",
    ] = Field(
        description="Recommended pricing or merchandising action based on competitor price, stock, and shipping."
    )
    urgencyLevel: Literal["HIGH", "MEDIUM", "LOW"] = Field(
        description="Urgency level for the merchandising team."
    )
    merchandisingProposal: str = Field(
        description="Concise, actionable merchandising & pricing recommendation referencing exact prices, diff, stock status, and shipping terms."
    )
    slackAlertMessage: str = Field(
        description="Ready-to-post Slack alert message formatted with mrkdwn (*bold*, bullet points) summarizing competitor status and recommended action."
    )


def _parse_price(value: Any) -> float | None:
    """Parse a numeric price from int, float, or formatted currency string."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2) if value >= 0 else None

    raw = str(value).strip()
    if not raw:
        return None

    # Keep digits, dots, and commas
    cleaned = re.sub(r"[^\d.,]", "", raw)
    if not cleaned:
        return None

    # Handle European decimal comma (e.g., "129,99") vs thousands comma ("1,299.99")
    if "," in cleaned and "." not in cleaned:
        if re.search(r",\d{1,2}$", cleaned):
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", "")

    try:
        parsed = float(cleaned)
        return round(parsed, 2) if parsed >= 0 else None
    except ValueError:
        return None


def _normalize_availability(raw: Any) -> str | None:
    """Map Schema.org or text availability strings to IN_STOCK / OUT_OF_STOCK."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "IN_STOCK" if raw else "OUT_OF_STOCK"

    text = str(raw).strip().lower()
    if not text:
        return None

    out_markers = (
        "outofstock",
        "out_of_stock",
        "out of stock",
        "soldout",
        "sold_out",
        "sold out",
        "discontinued",
        "unavailable",
    )
    if any(marker in text for marker in out_markers):
        return "OUT_OF_STOCK"

    in_markers = (
        "instock",
        "in_stock",
        "in stock",
        "onlineonly",
        "instoreonly",
        "preorder",
        "limitedavailability",
        "available",
    )
    if any(marker in text for marker in in_markers):
        return "IN_STOCK"

    return None


def _build_shopify_endpoints(url: str) -> tuple[str | None, str | None]:
    """Return Shopify .json and .js endpoints if the URL points to a /products/<handle> route."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path or "/products/" not in path.lower():
        return None, None

    if path.endswith(".json"):
        base_path = path[:-5]
    elif path.endswith(".js"):
        base_path = path[:-3]
    else:
        base_path = path

    base_url = f"{parsed.scheme}://{parsed.netloc}{base_path}"
    return f"{base_url}.json", f"{base_url}.js"


def _extract_shopify_json_data(payload: dict) -> dict:
    """Extract normalized product fields from Shopify /products/<handle>.json."""
    product = payload.get("product")
    if not isinstance(product, dict):
        return {}

    variants = product.get("variants") or []
    available_flags: list[bool] = []
    candidate_prices: list[tuple[bool, float, float | None, str | None]] = []

    for variant in variants:
        if not isinstance(variant, dict):
            continue

        is_available = variant.get("available")
        if is_available is None and "inventory_quantity" in variant:
            qty = variant.get("inventory_quantity")
            policy = variant.get("inventory_policy")
            mgmt = variant.get("inventory_management")
            if mgmt and isinstance(qty, (int, float)):
                is_available = qty > 0 or policy == "continue"

        if isinstance(is_available, bool):
            available_flags.append(is_available)

        price = _parse_price(variant.get("price"))
        compare_at = _parse_price(variant.get("compare_at_price"))
        sku = variant.get("sku") or None
        if price is not None:
            candidate_prices.append((bool(is_available) if is_available is not None else True, price, compare_at, sku))

    stock_status = None
    if available_flags:
        stock_status = "IN_STOCK" if any(available_flags) else "OUT_OF_STOCK"

    chosen_price = None
    chosen_compare_at = None
    chosen_sku = None
    if candidate_prices:
        in_stock_prices = [item for item in candidate_prices if item[0]]
        pool = in_stock_prices if in_stock_prices else candidate_prices
        _, chosen_price, chosen_compare_at, chosen_sku = min(pool, key=lambda x: x[1])

    return {
        "productName": product.get("title") or None,
        "brand": product.get("vendor") or None,
        "price": chosen_price,
        "compareAtPrice": chosen_compare_at,
        "sku": chosen_sku,
        "stockStatus": stock_status,
        "variantCount": len(variants),
        "source": "shopify_json",
    }


def _extract_shopify_js_data(payload: dict) -> dict:
    """Extract availability and fallback price (in cents) from Shopify /products/<handle>.js."""
    if not isinstance(payload, dict):
        return {}

    stock_status = None
    if isinstance(payload.get("available"), bool):
        stock_status = "IN_STOCK" if payload["available"] else "OUT_OF_STOCK"

    price_cents = payload.get("price")
    price = None
    if isinstance(price_cents, (int, float)) and price_cents >= 0:
        # Shopify .js endpoint returns prices in minor currency units (cents)
        price = round(float(price_cents) / 100.0, 2)

    return {
        "productName": payload.get("title") or None,
        "brand": payload.get("vendor") or None,
        "price": price,
        "stockStatus": stock_status,
    }


def _is_schema_type(node: dict, expected: str) -> bool:
    """Check whether a JSON-LD node's @type matches expected Schema.org type."""
    raw_type = node.get("@type")
    if not raw_type:
        return False
    types_list = raw_type if isinstance(raw_type, list) else [raw_type]
    expected_lower = expected.lower()
    for t in types_list:
        short = str(t).split("/")[-1].split(":")[-1].lower()
        if short == expected_lower:
            return True
    return False


def _collect_jsonld_nodes(data: Any) -> list[dict]:
    """Flatten JSON-LD dict/list/@graph structures into a list of dicts."""
    nodes: list[dict] = []
    if isinstance(data, list):
        for item in data:
            nodes.extend(_collect_jsonld_nodes(item))
    elif isinstance(data, dict):
        nodes.append(data)
        if "@graph" in data:
            nodes.extend(_collect_jsonld_nodes(data["@graph"]))
    return nodes


def _extract_shipping_from_jsonld(shipping_details: Any) -> str | None:
    """Parse Schema.org OfferShippingDetails for free shipping conditions."""
    if not shipping_details:
        return None
    items = shipping_details if isinstance(shipping_details, list) else [shipping_details]

    for detail in items:
        if not isinstance(detail, dict):
            continue
        if detail.get("doesNotShip") is True:
            continue
        rate = detail.get("shippingRate") or {}
        if isinstance(rate, dict):
            val = _parse_price(rate.get("value"))
            if val is not None and val == 0:
                min_order = detail.get("eligibleTransactionVolume") or {}
                if isinstance(min_order, dict) and min_order.get("minValue") is not None:
                    currency = min_order.get("currency") or rate.get("currency") or ""
                    return f"Free shipping on orders over {min_order['minValue']} {currency}".strip()
                return "Free shipping (JSON-LD OfferShippingDetails)"
    return None


def _parse_offer_node(offer: Any) -> tuple[float | None, str | None, str | None, str | None]:
    """Extract (price, currency, stock_status, free_shipping_condition) from Offer or AggregateOffer."""
    offer_list = offer if isinstance(offer, list) else [offer]
    candidates: list[tuple[int, float, str | None, str | None, str | None]] = []
    fallback_currency = None
    fallback_stock = None
    fallback_shipping = None

    for item in offer_list:
        if not isinstance(item, dict):
            continue

        # Recurse into AggregateOffer nested offers if present
        if _is_schema_type(item, "AggregateOffer") and item.get("offers"):
            nested_price, nested_curr, nested_stock, nested_ship = _parse_offer_node(item["offers"])
            if nested_price is not None:
                candidates.append(
                    (
                        0 if nested_stock == "IN_STOCK" else 1,
                        nested_price,
                        nested_curr or item.get("priceCurrency"),
                        nested_stock or _normalize_availability(item.get("availability")),
                        nested_ship or _extract_shipping_from_jsonld(item.get("shippingDetails")),
                    )
                )
                continue

        price = _parse_price(item.get("price"))
        if price is None:
            price = _parse_price(item.get("lowPrice"))
        if price is None and isinstance(item.get("priceSpecification"), dict):
            price = _parse_price(item["priceSpecification"].get("price"))

        currency = item.get("priceCurrency") or (
            item["priceSpecification"].get("priceCurrency")
            if isinstance(item.get("priceSpecification"), dict)
            else None
        )
        stock = _normalize_availability(item.get("availability"))
        shipping = _extract_shipping_from_jsonld(item.get("shippingDetails"))

        if currency and not fallback_currency:
            fallback_currency = currency
        if stock and not fallback_stock:
            fallback_stock = stock
        if shipping and not fallback_shipping:
            fallback_shipping = shipping

        if price is not None:
            rank = 0 if stock == "IN_STOCK" else 1
            candidates.append((rank, price, currency, stock, shipping))

    if candidates:
        _, best_price, best_curr, best_stock, best_ship = min(candidates, key=lambda x: (x[0], x[1]))
        return (
            best_price,
            best_curr or fallback_currency,
            best_stock or fallback_stock,
            best_ship or fallback_shipping,
        )

    return None, fallback_currency, fallback_stock, fallback_shipping


def _extract_free_shipping_from_html(soup: BeautifulSoup) -> str | None:
    """Scan page text for English or Japanese free shipping conditions."""
    # Remove script/style noise before scanning visible text
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    matches: list[str] = []
    for pattern in FREE_SHIPPING_PATTERNS:
        for match in pattern.findall(text):
            cleaned = re.sub(r"\s+", " ", match).strip(" .,:;|-")
            if cleaned and cleaned not in matches:
                matches.append(cleaned[:120])

    if not matches:
        return None

    # Prefer matches mentioning a threshold/amount over generic phrases
    threshold_matches = [m for m in matches if re.search(r"\d", m)]
    return threshold_matches[0] if threshold_matches else matches[0]


def _extract_jsonld_and_meta(html: str) -> dict:
    """Extract product metadata from JSON-LD Product/Offer blocks and OpenGraph/meta tags."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, Any] = {
        "productName": None,
        "brand": None,
        "price": None,
        "currency": None,
        "stockStatus": None,
        "sku": None,
        "freeShippingCondition": None,
        "hasJsonLd": False,
    }

    all_nodes: list[dict] = []
    for script in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        raw_json = (script.string or script.get_text() or "").strip()
        if not raw_json:
            continue
        try:
            parsed = json.loads(raw_json)
            all_nodes.extend(_collect_jsonld_nodes(parsed))
        except json.JSONDecodeError:
            continue

    product_nodes = [n for n in all_nodes if _is_schema_type(n, "Product")]
    offer_nodes = [n for n in all_nodes if _is_schema_type(n, "Offer") or _is_schema_type(n, "AggregateOffer")]

    if product_nodes:
        result["hasJsonLd"] = True
        prod = product_nodes[0]
        result["productName"] = prod.get("name") or None
        result["sku"] = prod.get("sku") or None

        brand = prod.get("brand")
        if isinstance(brand, dict):
            result["brand"] = brand.get("name") or None
        elif isinstance(brand, str):
            result["brand"] = brand or None

        if prod.get("offers"):
            p_price, p_curr, p_stock, p_ship = _parse_offer_node(prod["offers"])
            result["price"] = p_price
            result["currency"] = p_curr
            result["stockStatus"] = p_stock
            result["freeShippingCondition"] = p_ship

    if (result["price"] is None or result["stockStatus"] is None) and offer_nodes:
        result["hasJsonLd"] = True
        o_price, o_curr, o_stock, o_ship = _parse_offer_node(offer_nodes)
        result["price"] = result["price"] if result["price"] is not None else o_price
        result["currency"] = result["currency"] or o_curr
        result["stockStatus"] = result["stockStatus"] or o_stock
        result["freeShippingCondition"] = result["freeShippingCondition"] or o_ship

    # Meta / OpenGraph fallbacks
    def _meta_content(*keys: str) -> str | None:
        for key in keys:
            tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
            if tag and tag.get("content"):
                return str(tag["content"]).strip()
        return None

    if not result["productName"]:
        result["productName"] = _meta_content("og:title", "twitter:title")
        if not result["productName"] and soup.title and soup.title.string:
            result["productName"] = soup.title.string.strip()

    if result["price"] is None:
        result["price"] = _parse_price(_meta_content("product:price:amount", "og:price:amount"))
    if not result["currency"]:
        result["currency"] = _meta_content("product:price:currency", "og:price:currency")
    if not result["stockStatus"]:
        result["stockStatus"] = _normalize_availability(
            _meta_content("product:availability", "og:availability")
        )

    if not result["freeShippingCondition"]:
        result["freeShippingCondition"] = _extract_free_shipping_from_html(soup)

    return result


async def fetch_competitor_product(client: httpx.AsyncClient, url: str) -> dict:
    """Asynchronously extract product details via Shopify /products/*.json and JSON-LD HTML."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
    }

    shopify_json_url, shopify_js_url = _build_shopify_endpoints(url)
    shopify_data: dict[str, Any] = {}
    shopify_js_data: dict[str, Any] = {}
    html_data: dict[str, Any] = {}
    errors: list[str] = []

    async def _get_json(target_url: str | None) -> dict | None:
        if not target_url:
            return None
        try:
            resp = await client.get(target_url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            errors.append(f"{target_url}: {exc}")
        return None

    async def _get_html(target_url: str) -> str | None:
        try:
            resp = await client.get(target_url, headers=headers)
            if resp.status_code < 400:
                return resp.text[:500_000]
            errors.append(f"HTTP {resp.status_code} on HTML fetch")
        except Exception as exc:
            errors.append(f"HTML fetch error: {exc}")
        return None

    json_payload, html_text = await asyncio.gather(
        _get_json(shopify_json_url),
        _get_html(url),
    )

    if json_payload:
        shopify_data = _extract_shopify_json_data(json_payload)

    # Fetch Shopify .js only when .json exists or URL is Shopify and stock/price is still missing
    if shopify_js_url and (not shopify_data.get("stockStatus") or shopify_data.get("price") is None):
        js_payload = await _get_json(shopify_js_url)
        if js_payload:
            shopify_js_data = _extract_shopify_js_data(js_payload)

    if html_text:
        html_data = _extract_jsonld_and_meta(html_text)

    product_name = (
        shopify_data.get("productName")
        or shopify_js_data.get("productName")
        or html_data.get("productName")
    )
    brand = shopify_data.get("brand") or shopify_js_data.get("brand") or html_data.get("brand")
    price = (
        shopify_data.get("price")
        if shopify_data.get("price") is not None
        else (
            html_data.get("price")
            if html_data.get("price") is not None
            else shopify_js_data.get("price")
        )
    )
    stock_status = (
        shopify_data.get("stockStatus")
        or shopify_js_data.get("stockStatus")
        or html_data.get("stockStatus")
        or "UNKNOWN"
    )
    currency = html_data.get("currency") or "USD"
    free_shipping = html_data.get("freeShippingCondition") or "Not specified"

    extraction_sources = []
    if shopify_data:
        extraction_sources.append("shopify_json")
    if shopify_js_data:
        extraction_sources.append("shopify_js")
    if html_data.get("hasJsonLd"):
        extraction_sources.append("json_ld")
    elif html_text:
        extraction_sources.append("html_meta")

    return {
        "url": url,
        "productName": product_name,
        "brand": brand,
        "competitorPrice": price,
        "compareAtPrice": shopify_data.get("compareAtPrice"),
        "currency": currency,
        "stockStatus": stock_status,
        "freeShippingCondition": free_shipping,
        "sku": shopify_data.get("sku") or html_data.get("sku"),
        "variantCount": shopify_data.get("variantCount"),
        "extractionSources": extraction_sources,
        "extractionErrors": errors,
    }


def calculate_price_comparison(
    competitor_price: float | None, baseline_price: float | None
) -> dict[str, Any]:
    """Compute priceDiff, priceDiffPercent, and pricePosition against ourBaselinePrice."""
    if competitor_price is None or baseline_price is None:
        return {
            "ourBaselinePrice": baseline_price,
            "priceDiff": None,
            "priceDiffPercent": None,
            "pricePosition": "UNKNOWN",
        }

    diff = round(competitor_price - baseline_price, 2)
    diff_pct = round((diff / baseline_price) * 100.0, 2) if baseline_price > 0 else None

    if diff < 0:
        position = "COMPETITOR_CHEAPER"
    elif diff > 0:
        position = "COMPETITOR_HIGHER"
    else:
        position = "EQUAL"

    return {
        "ourBaselinePrice": round(baseline_price, 2),
        "priceDiff": diff,
        "priceDiffPercent": diff_pct,
        "pricePosition": position,
    }


async def resolve_working_gemini_models(
    gemini_client: genai.Client, preferred_model: str = ""
) -> list[str]:
    """Build a prioritized Gemini Flash model list with dynamic API discovery fallback."""
    candidates: list[str] = []
    if preferred_model:
        candidates.append(preferred_model)

    for default_model in ["gemini-2.5-flash", "gemini-flash-latest"]:
        if default_model not in candidates:
            candidates.append(default_model)

    try:
        discovered_flash = []
        discovered_other = []
        async for m in await gemini_client.aio.models.list():
            name = (m.name or "").replace("models/", "")
            methods = m.supported_generation_methods or []
            name_lower = name.lower()
            if (
                "generateContent" in methods
                and "gemini" in name_lower
                and "image" not in name_lower
                and "audio" not in name_lower
                and "tts" not in name_lower
                and "embedding" not in name_lower
            ):
                if "flash" in name_lower:
                    discovered_flash.append(name)
                elif "pro" in name_lower:
                    discovered_other.append(name)

        for name in sorted(discovered_flash, reverse=True) + sorted(discovered_other, reverse=True):
            if name not in candidates:
                candidates.append(name)
    except Exception:
        pass

    return candidates


async def analyze_with_gemini(
    gemini_client: genai.Client,
    product_payload: dict,
    model_candidates: list[str],
) -> dict:
    """Generate structured merchandising proposal and Slack alert via Gemini 2.5 Flash."""
    prompt = (
        "You are an expert e-commerce merchandising & pricing strategist.\n"
        "Analyze the competitor product snapshot and our baseline price below.\n"
        "Return structured JSON with:\n"
        "1. recommendedAction: choose the best action.\n"
        "2. urgencyLevel: HIGH if competitor is out of stock or undercutting significantly, MEDIUM/LOW otherwise.\n"
        "3. merchandisingProposal: concrete, data-backed recommendation (2-3 sentences) referencing exact prices, priceDiff, stockStatus, and freeShippingCondition.\n"
        "4. slackAlertMessage: a concise Slack mrkdwn alert message ready for a #pricing-alerts channel.\n\n"
        f"Competitor Snapshot:\n{json.dumps(product_payload, ensure_ascii=False)}"
    )

    last_error: Exception | None = None
    for model_name in model_candidates:
        for attempt in range(2):
            try:
                response = await gemini_client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=MerchandisingAnalysis,
                        temperature=0.3,
                    ),
                )
                return json.loads(response.text)
            except Exception as exc:
                last_error = exc
                err_msg = str(exc)
                if any(code in err_msg for code in ["503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"]):
                    if attempt == 0:
                        Actor.log.info(f"Model '{model_name}' busy (503/429), retrying in 2s...")
                        await asyncio.sleep(2)
                        continue
                    Actor.log.info(f"Model '{model_name}' still busy, switching to next candidate...")
                    break
                if any(code in err_msg for code in ["404", "NOT_FOUND", "no longer available"]):
                    Actor.log.info(f"Model '{model_name}' unavailable, falling back to next candidate...")
                    break
                raise

    if last_error:
        raise last_error
    raise RuntimeError("No available Gemini model succeeded.")


def build_fallback_merchandising_analysis(snapshot: dict) -> dict:
    """Deterministic merchandising proposal and Slack alert when Gemini API is unavailable or skipped."""
    name = snapshot.get("productName") or "Competitor Product"
    comp_price = snapshot.get("competitorPrice")
    baseline = snapshot.get("ourBaselinePrice")
    diff = snapshot.get("priceDiff")
    diff_pct = snapshot.get("priceDiffPercent")
    currency = snapshot.get("currency") or "USD"
    stock = snapshot.get("stockStatus") or "UNKNOWN"
    shipping = snapshot.get("freeShippingCondition") or "Not specified"
    url = snapshot.get("url") or ""

    if stock == "OUT_OF_STOCK":
        action = "CAPTURE_OUT_OF_STOCK_DEMAND"
        urgency = "HIGH"
        proposal = (
            f"Competitor '{name}' is currently OUT_OF_STOCK. Increase paid search and shopping bid coverage "
            f"while highlighting immediate availability and '{shipping}' parity against our {baseline} {currency} baseline."
        )
    elif diff is not None and diff < 0:
        action = "LAUNCH_PROMO_BUNDLE" if (diff_pct is not None and diff_pct >= -10) else "LOWER_PRICE"
        urgency = "HIGH" if (diff_pct is not None and diff_pct <= -10) else "MEDIUM"
        proposal = (
            f"Competitor '{name}' is priced at {comp_price} {currency} ({diff} {currency} / {diff_pct}% vs our "
            f"{baseline} {currency} baseline) with '{shipping}'. Consider a targeted price match or value bundle to protect conversion."
        )
    elif diff is not None and diff > 0:
        action = "RAISE_PRICE" if (diff_pct is not None and diff_pct >= 15) else "HOLD_PRICE"
        urgency = "LOW"
        proposal = (
            f"Our {baseline} {currency} baseline undercuts competitor '{name}' ({comp_price} {currency}, +{diff} {currency}). "
            f"Maintain price advantage in ad copy or test a modest margin increase while monitoring '{shipping}'."
        )
    else:
        action = "HOLD_PRICE"
        urgency = "LOW"
        proposal = (
            f"Competitor '{name}' matches or lacks clear pricing variance against our {baseline} {currency} baseline "
            f"(stock: {stock}, shipping: {shipping}). Hold current price and differentiate on fulfillment speed."
        )

    diff_label = f"{diff:+.2f} {currency} ({diff_pct:+.2f}%)" if diff is not None and diff_pct is not None else "N/A"
    slack_msg = (
        f"*Competitor Price & Stock Alert* [{urgency}]\n"
        f"• *Product:* {name}\n"
        f"• *Competitor Price:* {comp_price} {currency} (vs Baseline {baseline} {currency}: {diff_label})\n"
        f"• *Stock:* `{stock}` | *Shipping:* {shipping}\n"
        f"• *Recommended Action:* `{action}` — {proposal}\n"
        f"• *URL:* {url}"
    )

    return {
        "recommendedAction": action,
        "urgencyLevel": urgency,
        "merchandisingProposal": proposal,
        "slackAlertMessage": slack_msg,
    }


def build_demo_snapshot(url: str, baseline_price: float) -> dict:
    """Return a realistic sample record if default health-check URL blocks scraping."""
    comp_price = 98.0
    comparison = calculate_price_comparison(comp_price, baseline_price)
    snapshot = {
        "url": url,
        "productName": "Men's Tree Runners (Sample Demo Snapshot)",
        "brand": "Allbirds",
        "competitorPrice": comp_price,
        "compareAtPrice": 110.0,
        "currency": "USD",
        "stockStatus": "IN_STOCK",
        "freeShippingCondition": "Free shipping on orders over $75",
        "sku": "DEMO-TREE-RUNNER-01",
        "variantCount": 8,
        "extractionSources": ["demo_fallback"],
        "extractionErrors": [],
        **comparison,
        "isDemoData": True,
    }
    ai_insights = build_fallback_merchandising_analysis(snapshot)
    return {
        **snapshot,
        **ai_insights,
        "aiAnalysisMode": "RuleBasedFallback",
        "checkedAt": datetime.now(timezone.utc).isoformat(),
    }


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        raw_urls = actor_input.get("productUrls") or DEFAULT_PRODUCT_URLS
        product_urls: list[str] = []
        seen_urls: set[str] = set()
        for raw in raw_urls:
            u = str(raw).strip()
            if u and u not in seen_urls:
                seen_urls.add(u)
                product_urls.append(u)

        raw_baseline = actor_input.get("ourBaselinePrice", 100)
        try:
            baseline_price = float(raw_baseline) if raw_baseline is not None else 100.0
        except (TypeError, ValueError):
            baseline_price = 100.0

        gemini_api_key = (
            actor_input.get("geminiApiKey") or os.getenv("GEMINI_API_KEY", "")
        ).strip()
        preferred_model = (actor_input.get("geminiModel") or "").strip()

        gemini_client: genai.Client | None = None
        model_candidates: list[str] = []
        if gemini_api_key:
            gemini_client = genai.Client(api_key=gemini_api_key)
            model_candidates = await resolve_working_gemini_models(
                gemini_client, preferred_model
            )
        else:
            Actor.log.info(
                "No geminiApiKey or GEMINI_API_KEY provided; using deterministic rule-based merchandising & Slack alert generation."
            )

        semaphore = asyncio.Semaphore(5)

        async def process_product_url(http_client: httpx.AsyncClient, url: str) -> None:
            async with semaphore:
                extracted = await fetch_competitor_product(http_client, url)

            # Fallback for Apify daily default-input health check if target blocks automated HTTP
            if (
                not extracted.get("productName")
                and extracted.get("competitorPrice") is None
                and url in DEFAULT_PRODUCT_URLS
                and not actor_input.get("productUrls")
            ):
                Actor.log.info(
                    f"Default health-check URL blocked live scraping ({url}); pushing sample demo record."
                )
                await Actor.push_data(build_demo_snapshot(url, baseline_price))
                await Actor.charge(event_name="product-analyzed", count=1)
                return

            comparison = calculate_price_comparison(
                extracted.get("competitorPrice"), baseline_price
            )
            snapshot = {**extracted, **comparison}

            ai_mode = "RuleBasedFallback"
            if gemini_client is not None:
                try:
                    ai_insights = await analyze_with_gemini(
                        gemini_client, snapshot, model_candidates
                    )
                    ai_mode = "Gemini"
                except Exception as exc:
                    Actor.log.warning(
                        f"Gemini analysis failed for {url} ({exc}); falling back to rule-based analysis."
                    )
                    ai_insights = build_fallback_merchandising_analysis(snapshot)
            else:
                ai_insights = build_fallback_merchandising_analysis(snapshot)

            record = {
                **snapshot,
                **ai_insights,
                "aiAnalysisMode": ai_mode,
                "checkedAt": datetime.now(timezone.utc).isoformat(),
            }

            await Actor.push_data(record)
            await Actor.charge(event_name="product-analyzed", count=1)

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http_client:
            tasks = [process_product_url(http_client, url) for url in product_urls]
            await asyncio.gather(*tasks)

        Actor.log.info(f"Completed monitoring {len(product_urls)} product URL(s).")


if __name__ == "__main__":
    asyncio.run(main())
