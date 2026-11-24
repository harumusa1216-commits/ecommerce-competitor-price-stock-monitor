# E-Commerce Competitor Price & Stock Monitor (Shopify + JSON-LD + AI Alerts)

Monitors Shopify and JSON-LD product pages using lightweight HTTP requests (no headless browser) to track price drops, stock changes, and shipping thresholds, with optional AI merchandising alerts.

Pricing starts at $2.00 per 1,000 products (down to $1.00/1,000 on higher Apify plans). A 10-product test run costs roughly $0.02, which fits easily within Apify's $5 monthly free credit.

## Quick Output Preview

| Product (`productName`) | Price | Stock (`stockStatus`) | Shipping | Action (`recommendedAction`) |
| :--- | :--- | :--- | :--- | :--- |
| Men's Tree Runners | $98.00 (-2.0%) | `IN_STOCK` | Free over $75 | `LAUNCH_PROMO_BUNDLE` |
| Wireless Earbuds Pro | $79.00 (-15.0%) | `OUT_OF_STOCK` | Free over $50 | `CAPTURE_STOCKOUT_DEMAND` |

---

## Key Features

1. **Dual-Engine Product Extraction (Shopify JSON + Schema.org JSON-LD)**
   - **Shopify `/products/*.json` (and `.js` availability fallback)**: Automatically probes Shopify product JSON endpoints to extract product title, brand, SKU, variant count, price, `compareAtPrice`, and stock status.
   - **Schema.org JSON-LD (`Product` / `Offer` / `AggregateOffer`) + OpenGraph Fallback**: Works across non-Shopify storefronts by parsing `<script type="application/ld+json">` blocks and meta tags.
2. **Accurate Stock & Free Shipping Intelligence**
   - Normalizes inventory into `IN_STOCK`, `OUT_OF_STOCK`, or `UNKNOWN`.
   - Extracts free shipping conditions from JSON-LD `OfferShippingDetails` and English/Japanese storefront shipping banners (e.g., `"Free shipping on orders over $75"`, `"5,000円以上で送料無料"`).
3. **Baseline Price Gap Calculation**
   - Compares each competitor's live price against your `ourBaselinePrice` and outputs `priceDiff`, `priceDiffPercent`, and `pricePosition` (`COMPETITOR_CHEAPER`, `COMPETITOR_HIGHER`, `EQUAL`, `UNKNOWN`).
4. **Gemini 2.5 Flash Merchandising & Slack Alerts (with Built-In Fallback)**
   - Outputs structured recommendations (`recommendedAction`, `urgencyLevel`, `merchandisingProposal`, and `slackAlertMessage`) via `gemini-2.5-flash` (with automatic model fallback and rule-based fallback if no API key is supplied).
5. **Fast, Low-Cost, Browserless Architecture**
   - Runs purely on asynchronous `httpx` requests without launching heavy headless browsers, keeping Apify compute usage ultra-low.

---

## Input Parameters

| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `productUrls` | `string[]` | `["https://allbirds.com/products/mens-tree-runners"]` | List of competitor product URLs (Shopify `/products/*` or any JSON-LD store). |
| `ourBaselinePrice` | `integer` | `100` | Your baseline product price for computing `priceDiff` and `priceDiffPercent`. |
| `geminiApiKey` | `string` (secret) | `""` | Optional Google Gemini API key (BYOK). Falls back to `GEMINI_API_KEY` env var if set. |
| `geminiModel` | `string` | `""` | Optional Gemini model override (defaults to `gemini-2.5-flash`). |

---

## Sample Output (Dataset Item)

```json
{
  "url": "[https://allbirds.com/products/mens-tree-runners](https://allbirds.com/products/mens-tree-runners)",
  "productName": "Men's Tree Runners",
  "brand": "Allbirds",
  "competitorPrice": 98.0,
  "compareAtPrice": 110.0,
  "currency": "USD",
  "stockStatus": "IN_STOCK",
  "freeShippingCondition": "Free shipping on orders over $75",
  "sku": "A10012",
  "variantCount": 8,
  "extractionSources": ["shopify_json", "json_ld"],
  "extractionErrors": [],
  "ourBaselinePrice": 100.0,
  "priceDiff": -2.0,
  "priceDiffPercent": -2.0,
  "pricePosition": "COMPETITOR_CHEAPER",
  "recommendedAction": "LAUNCH_PROMO_BUNDLE",
  "urgencyLevel": "MEDIUM",
  "merchandisingProposal": "Competitor Men's Tree Runners is priced at 98.0 USD (-2.0 USD / -2.0% vs our 100.0 USD baseline) with free shipping over $75. Launch a limited-time bundle or match free-shipping parity rather than cutting list price directly.",
  "slackAlertMessage": "*Competitor Price & Stock Alert* [MEDIUM]\n• *Product:* Men's Tree Runners\n• *Competitor Price:* 98.0 USD (vs Baseline 100.0 USD: -2.00 USD (-2.00%))\n• *Stock:* `IN_STOCK` | *Shipping:* Free shipping on orders over $75\n• *Recommended Action:* `LAUNCH_PROMO_BUNDLE`",
  "aiAnalysisMode": "Gemini",
  "checkedAt": "2026-11-16T15:30:00+00:00"
}
