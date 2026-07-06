"""Orchestrator for the RUCHI recommendation automation.

Modes:
  Preview (no credentials, no writes):
    python -m src.main --source xlsx --xlsx export.xlsx --out out/
  Production (GitHub Actions):
    python -m src.main --source api --write-shopify --write-klaviyo --out out/
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

import yaml

from .scoring import Product, build_recommendations, derive_families, load_family_overrides

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_from_xlsx(path: str, prices_path: str | None = None,
                   category_path: str | None = None) -> list[Product]:
    """Load products from a Matrixify-style export. Columns are located by header
    name, so metafield and price/inventory columns can live in one file or be
    supplied as a second export (joined on product ID)."""
    import openpyxl

    def read_sheet(p):
        ws = openpyxl.load_workbook(p, read_only=True)["Products"]
        rows = ws.iter_rows(values_only=True)
        header = [str(h or "") for h in next(rows)]
        idx = {h: i for i, h in enumerate(header)}
        return idx, list(rows)

    def col(idx, row, *names):
        for n in names:
            for h, i in idx.items():
                if n.lower() in h.lower():
                    return row[i]
        return None

    seen: dict[str, Product] = {}
    idx, rows = read_sheet(path)
    for r in rows:
        pid = str(r[idx["ID"]])
        if pid in seen and seen[pid].stone:
            continue  # variant rows repeat product; keep first row with metafields
        def mv(v):  # 'stone.blue-sapphire' -> 'blue-sapphire'
            return str(v).split(".", 1)[-1] if v else None
        handle = str(r[idx["Handle"]] or "")
        seen[pid] = Product(
            id=pid, handle=handle, title=str(r[idx["Title"]] or ""),
            stone=mv(col(idx, r, "custom.stone")),
            metal=mv(col(idx, r, "custom.metal_color")),
            style=mv(col(idx, r, "custom.style ", "custom.style [")),
            url=f"https://ruchinewyork.com/products/{handle}",
        )

    # Merge price + inventory (min variant price, summed inventory)
    pidx, prows = (read_sheet(prices_path) if prices_path else (idx, rows))
    has_price = any("price" in h.lower() for h in pidx)
    if has_price:
        inv_seen: dict[str, int] = {}
        for r in prows:
            pid = str(r[pidx["ID"]])
            if pid not in seen:
                continue
            price = col(pidx, r, "Variant Price")
            qty = col(pidx, r, "Inventory Qty")
            if price is not None:
                p = float(price)
                if seen[pid].price is None or p < seen[pid].price:
                    seen[pid].price = p
            if qty is not None:
                inv_seen[pid] = inv_seen.get(pid, 0) + int(qty)
        for pid, total in inv_seen.items():
            seen[pid].available = total > 0

    # Merge explicit category ("Category: Name" column) if provided
    if category_path:
        cidx, crows = read_sheet(category_path)
        for r in crows:
            pid = str(r[cidx["ID"]])
            if pid in seen and seen[pid].category is None:
                cat = col(cidx, r, "Category: Name")
                if cat:
                    seen[pid].category = str(cat)
    return list(seen.values())


def write_preview_csv(path: str, recs: dict, products_by_id: dict):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product", "stone", "metal", "category", "family", "price",
                    "rank", "recommended", "rec_stone", "rec_metal", "rec_category",
                    "rec_family", "rec_price"])
        for pid, rec_products in recs.items():
            t = products_by_id[pid]
            for rank, r in enumerate(rec_products, 1):
                w.writerow([t.title, t.stone, t.metal, t.category, t.family, t.price,
                            rank, r.title, r.stone, r.metal, r.category, r.family, r.price])


def write_klaviyo_feed(path: str, recs: dict, products_by_id: dict, n: int):
    """JSON web feed keyed by product id: templates use feeds.X|lookup:ProductID."""
    import json
    feed = {}
    for pid, rec_products in recs.items():
        if not rec_products:
            continue
        feed[pid] = [{
            "title": r.title,
            "url": r.url,
            "image": r.image,
            "price": f"${r.price:,.0f}" if r.price else "",
        } for r in rec_products[:n]]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(feed, f, separators=(",", ":"))


def write_review_csv(path: str, rows: list[dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["handle", "title", "suggested_family", "reason"])
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["xlsx", "api"], default="api")
    ap.add_argument("--xlsx", default="export.xlsx")
    ap.add_argument("--prices-xlsx", default=None,
                    help="optional second export containing Variant Price / Inventory Qty")
    ap.add_argument("--category-xlsx", default=None,
                    help="optional export containing 'Category: Name'")
    ap.add_argument("--out", default="out")
    ap.add_argument("--write-shopify", action="store_true")
    ap.add_argument("--write-klaviyo", action="store_true")
    args = ap.parse_args()

    with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    os.makedirs(args.out, exist_ok=True)

    # 1. Load products
    if args.source == "xlsx":
        products = load_from_xlsx(args.xlsx, args.prices_xlsx, args.category_xlsx)
        shopify = None
    else:
        from .shopify_client import ShopifyClient
        shopify = ShopifyClient(cfg)
        products = shopify.fetch_products()
    print(f"Loaded {len(products)} products ({args.source})")

    # 2. Style families (overrides file wins over heuristic) + categories
    from .scoring import derive_categories
    overrides = load_family_overrides(os.path.join(HERE, "data", "style_family_overrides.csv"))
    review = derive_families(products, overrides, cfg["generic_first_words"])
    write_review_csv(os.path.join(args.out, "style_family_review.csv"), review)
    uncategorized = derive_categories(products, cfg)

    # 3. Score
    products_by_id = {p.id: p for p in products}
    recs = build_recommendations(products, cfg)
    with_recs = sum(1 for v in recs.values() if v)
    write_preview_csv(os.path.join(args.out, "recommendations_preview.csv"), recs, products_by_id)
    os.makedirs(os.path.join(HERE, "feed"), exist_ok=True)
    write_klaviyo_feed(os.path.join(HERE, "feed", "klaviyo_recs_feed.json"),
                       recs, products_by_id, cfg["klaviyo_recs_per_product"])

    # 4. Writes
    summary = [f"Products: {len(products)}",
               f"Products with recommendations: {with_recs}",
               f"Style families needing review: {len(review)}",
               f"Products without derivable category: {uncategorized}"]
    failed = False
    if args.write_shopify:
        written, errors = shopify.write_related_products(recs, products_by_id)
        summary.append(f"Shopify related_products written: {written}")
        if errors:
            failed = True
            summary.append(f"Shopify errors ({len(errors)}): " + "; ".join(errors[:5]))
    if args.write_klaviyo:
        from .klaviyo_client import KlaviyoClient
        synced, errors = KlaviyoClient(cfg).sync(recs, products_by_id)
        summary.append(f"Klaviyo catalog items synced: {synced}")
        if errors:
            failed = True
            summary.append(f"Klaviyo errors ({len(errors)}): " + "; ".join(errors[:5]))

    report = "\n".join(summary)
    print(report)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("## Recommendation sync\n```\n" + report + "\n```\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
