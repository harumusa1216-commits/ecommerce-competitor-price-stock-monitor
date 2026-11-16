# E-Commerce Competitor Price & Stock Monitor (Shopify + JSON-LD + AI Alerts)

Stop checking competitor product pages manually—or building fragile browser scrapers just to track price drops, stockouts, and shipping promos.

**E-Commerce Competitor Price & Stock Monitor** asynchronously fetches competitor product URLs using lightweight HTTP (`httpx` + `BeautifulSoup`), extracts structured data from **Shopify `/products/*.json` endpoints** and **Schema.org JSON-LD (`Product` / `Offer`)**, calculates exact price differences against your `ourBaselinePrice`, and uses **Gemini 2.5 Flash** to generate actionable **merchandising proposals** and ready-to-post **Slack alerts**.

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
  "url": "https://allbirds.com/products/mens-tree-runners",
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
```

---

## Pricing (Pay-Per-Event)

This Actor uses transparent **Pay-Per-Event** monetization:
- **`product-analyzed`** (`count=1`): Charged once per product URL analyzed and pushed to the dataset.

---

## Integration with Slack, n8n, Make, and AI Agents (MCP)

1. **Automated Slack Alerts (n8n / Make)**:
   - Schedule this Actor daily or hourly via Apify Schedules or n8n's Apify node.
   - Filter items where `urgencyLevel == "HIGH"` (e.g., `OUT_OF_STOCK` or major price undercut) and forward `slackAlertMessage` straight to a Slack Incoming Webhook.
2. **AI Agent Integration (Apify MCP)**:
   - Connect via `https://mcp.apify.com` so your AI merchandising assistant can inspect live competitor prices, stockouts, and shipping offers on demand.
