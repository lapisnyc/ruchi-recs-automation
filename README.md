# RUCHI New York: Recommendation Automation

Rule-based "You May Also Like" recommendations, computed nightly and pushed to
both the Shopify storefront and Klaviyo emails from one scoring engine.

Priority: **same stone + metal color > same category (earrings, rings...) >
same design name (Silla, Arles...) > similar price point**, with a hard cap so
a $70k piece never appears next to a $7k piece. Category comes from Shopify's
product taxonomy where set, otherwise it is derived from title keywords
(configurable in `config.yaml`).

## How it works

1. Pulls all active products (title, metafields, price, inventory) via the Shopify Admin GraphQL API
2. Derives style families (Arles, Lyre, Como...) from titles, with a human-editable overrides file
3. Scores every product pair with the weighted rules in `config.yaml`
4. Writes the top 10 per product to Shopify's Search & Discovery related-products metafield.
   The theme's "You May Also Like" section (native `recommendations` object) picks these up automatically. No theme changes.
5. Syncs the top 4 per product to a Klaviyo custom catalog for use in email templates
6. New products are picked up automatically on the next nightly run

## One-time setup

### 1. Shopify app + token (15 minutes)
Since January 2026, custom apps are created in the Dev Dashboard (dev.shopify.com),
not the store admin, and the token comes from a one-time OAuth exchange:

1. dev.shopify.com > correct organization > Apps > Create app ("RUCHI Recs Automation")
2. API access > request scopes `read_products`, `write_products`; add
   `http://localhost:3000/callback` to Allowed redirection URLs; release the version
3. Copy the Client ID and Client secret from app settings
4. Distribution > Custom distribution > enter the store's myshopify domain > install the app on the store via the generated link
5. Run `python scripts/get_token.py` and follow the prompts; it prints the permanent
   offline `shpat_` token (does not expire unless the app is uninstalled)

Do not use the dashboard's "App Automation Tokens" for this; those expire in 1-6 months.

### 2. Klaviyo private key
Klaviyo > Settings > API keys > Create Private API Key with Catalogs full access.

### 3. GitHub repository
Push this folder to a private repo. Add repository secrets (Settings > Secrets and variables > Actions):

| Secret | Value |
|---|---|
| `SHOPIFY_STORE` | `ruchi-new-york.myshopify.com` |
| `SHOPIFY_ADMIN_TOKEN` | token from step 1 |
| `KLAVIYO_API_KEY` | key from step 2 |

The workflow runs nightly at 6:00 UTC and can be triggered manually (Actions > Nightly recommendation sync > Run workflow). Each run uploads `recommendations_preview.csv` and `style_family_review.csv` as artifacts and prints a summary.

### 4. Style family review (1 hour, once)
Run a preview (below) and open `out/style_family_review.csv`. For any product where the
suggested family is wrong or blank, add a row to `data/style_family_overrides.csv`
(`handle,family`). Commit. Overrides always win; unlisted new products use the heuristic.

### 5. QA before first full write
- Run preview and spot-check `recommendations_preview.csv` for 10-15 products
- In the Search & Discovery app, confirm one product's related products after the first run
- Load its product page: the "You May Also Like" carousel should show the computed picks

## Running locally

```bash
pip install -r requirements.txt

# Preview only, no credentials, no writes (uses a Matrixify/export xlsx):
python -m src.main --source xlsx --xlsx export.xlsx --out out

# Full production run:
export SHOPIFY_STORE=... SHOPIFY_ADMIN_TOKEN=... KLAVIYO_API_KEY=...
python -m src.main --source api --write-shopify --write-klaviyo --out out
```

## Using the recommendations in Klaviyo emails (web feed)

Klaviyo blocks its catalog write API on accounts with an active Shopify catalog
sync ("You have at least one active Catalog Sync"), so recommendations are
delivered as a **web feed** instead. Each nightly run commits
`feed/klaviyo_recs_feed.json` to this repo: a JSON object keyed by Shopify
product id, each value a list of recs with `title`, `url`, `image`, `price`.

### One-time Klaviyo setup
1. Make the feed URL publicly reachable. Easiest: make this repo public
   (it contains only code and already-public product data), giving:
   `https://raw.githubusercontent.com/<user>/<repo>/main/feed/klaviyo_recs_feed.json`
   If the repo must stay private, publish the feed file to a separate tiny public
   repo instead.
2. Klaviyo > Settings > Other > Web feeds > Add web feed:
   name `RuchiRecs`, the URL above, method GET, content type JSON.

### In a flow email (e.g. Viewed Product / browse abandonment)
```django
{% with recs=feeds.RuchiRecs|lookup:event.ProductID %}
  <table><tr>
    {% for r in recs %}
      <td align="center"><a href="{{ r.url }}">
        <img src="{{ r.image }}" width="140"><br>
        {{ r.title }}<br>{{ r.price }}</a></td>
    {% endfor %}
  </tr></table>
{% endwith %}
```

Notes:
- The event property holding the product id varies by flow trigger
  (`event.ProductID` for Shopify Viewed Product; check the event payload). Adjust the lookup key.
- For campaigns (no triggering event), first add a flow step that writes the
  Viewed Product id to a profile property (e.g. `Last Viewed Product ID`), then
  use `feeds.RuchiRecs|lookup:person|lookup:'Last Viewed Product ID'`.
- Preview with a real profile before sending; feeds are fetched at send time.

## Configuration

Everything tunable lives in `config.yaml`: weights, price cap (default 2.5x),
counts, API versions, and the list of generic first words excluded from
style-family derivation. If DevTools ever shows the theme requesting
`intent=complementary` instead of `related`, change `shopify.metafield_key`
to `complementary_products`.

## Costs

$0/month. GitHub Actions free tier covers a nightly 2-3 minute run many times over.
Shopify and Klaviyo API usage is free.
