"""Klaviyo custom catalog sync.

Pushes each product with its top-N recommendations as custom_metadata fields
(rec1_title, rec1_url, rec1_image, rec1_price, ...) so email templates can
render them with a {% catalog %} lookup keyed on the Shopify product id.
"""
from __future__ import annotations

import os
import time

import requests

from .scoring import Product

BASE = "https://a.klaviyo.com/api"


class KlaviyoClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.headers = {
            "Authorization": f"Klaviyo-API-Key {os.environ['KLAVIYO_API_KEY']}",
            "revision": cfg["klaviyo"]["api_revision"],
            "Content-Type": "application/json",
            "accept": "application/json",
        }

    def _item_payload(self, product: Product, recs: list[Product]) -> dict:
        meta = {}
        for i, r in enumerate(recs, 1):
            meta[f"rec{i}_title"] = r.title
            meta[f"rec{i}_url"] = r.url
            meta[f"rec{i}_image"] = r.image
            meta[f"rec{i}_price"] = f"${r.price:,.0f}" if r.price else ""
        return {
            "type": "catalog-item",
            "attributes": {
                "external_id": product.id,
                "title": product.title,
                "url": product.url,
                "image_full_url": product.image or None,
                "price": product.price,
                "published": True,
                "custom_metadata": meta,
            },
        }

    def _bulk_job(self, kind: str, items: list[dict]) -> str:
        url = f"{BASE}/catalog-item-bulk-{kind}-jobs"
        payload = {"data": {
            "type": f"catalog-item-bulk-{kind}-job",
            "attributes": {"items": {"data": items}},
        }}
        for attempt in range(5):
            resp = requests.post(url, headers=self.headers, json=payload, timeout=60)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (200, 201, 202):
                return resp.json()["data"]["id"]
            # 409 on create means items exist -> caller falls back to update
            resp.raise_for_status()
        raise RuntimeError("Klaviyo rate-limited after retries")

    def sync(self, recs: dict[str, list[Product]],
             products_by_id: dict[str, Product]) -> tuple[int, list[str]]:
        """Upsert all products. Tries update first (normal case), creates the rest."""
        n = self.cfg["klaviyo"]["recs_per_product"] if "recs_per_product" in self.cfg["klaviyo"] \
            else self.cfg["klaviyo_recs_per_product"]
        batch_size = self.cfg["klaviyo"]["bulk_batch_size"]
        prefix = self.cfg["klaviyo"]["catalog_prefix"]

        existing = self._existing_ids()
        updates, creates = [], []
        for pid, rec_products in recs.items():
            if not rec_products:
                continue
            item = self._item_payload(products_by_id[pid], rec_products[:n])
            catalog_id = f"{prefix}{pid}"
            if catalog_id in existing:
                item["id"] = catalog_id
                updates.append(item)
            else:
                creates.append(item)

        synced, errors = 0, []
        for kind, items in (("update", updates), ("create", creates)):
            for i in range(0, len(items), batch_size):
                batch = items[i:i + batch_size]
                try:
                    self._bulk_job(kind, batch)
                    synced += len(batch)
                except Exception as e:  # keep going; report at the end
                    errors.append(f"{kind} batch {i // batch_size}: {e}")
        return synced, errors

    def _existing_ids(self) -> set[str]:
        ids, url = set(), f"{BASE}/catalog-items?fields[catalog-item]=external_id"
        while url:
            resp = requests.get(url, headers=self.headers, timeout=60)
            if resp.status_code == 429:
                time.sleep(2)
                continue
            resp.raise_for_status()
            body = resp.json()
            ids.update(item["id"] for item in body.get("data", []))
            url = (body.get("links") or {}).get("next")
        return ids
