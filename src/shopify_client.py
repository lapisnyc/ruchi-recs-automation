"""Shopify Admin GraphQL client: product fetch + Search & Discovery metafield writes."""
from __future__ import annotations

import json
import os
import time

import requests

from .scoring import Product

PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 250, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      legacyResourceId
      handle
      title
      onlineStorePreviewUrl
      totalInventory
      category { name }
      featuredMedia { preview { image { url } } }
      priceRangeV2 { minVariantPrice { amount } }
      metafields(first: 10, keys: ["custom.stone", "custom.metal_color", "custom.style", "custom.style_color"]) {
        nodes {
          key
          reference { ... on Metaobject { handle type } }
        }
      }
    }
  }
}
"""

METAFIELDS_SET_MUTATION = """
mutation($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id }
    userErrors { field message }
  }
}
"""


class ShopifyClient:
    def __init__(self, cfg: dict):
        store = os.environ["SHOPIFY_STORE"]  # e.g. ruchi-new-york.myshopify.com
        token = os.environ["SHOPIFY_ADMIN_TOKEN"]
        version = cfg["shopify"]["api_version"]
        self.cfg = cfg
        self.endpoint = f"https://{store}/admin/api/{version}/graphql.json"
        self.headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    def _gql(self, query: str, variables: dict) -> dict:
        for attempt in range(5):
            resp = requests.post(self.endpoint, headers=self.headers,
                                 json={"query": query, "variables": variables}, timeout=60)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            body = resp.json()
            if "errors" in body:
                throttled = any(e.get("extensions", {}).get("code") == "THROTTLED"
                                for e in body["errors"])
                if throttled:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"GraphQL errors: {body['errors']}")
            return body["data"]
        raise RuntimeError("Rate-limited after 5 retries")

    def fetch_products(self) -> list[Product]:
        products, cursor = [], None
        while True:
            data = self._gql(PRODUCTS_QUERY, {"cursor": cursor})
            page = data["products"]
            for node in page["nodes"]:
                metas = {m["key"]: (m.get("reference") or {}).get("handle")
                         for m in node["metafields"]["nodes"]}
                price = node.get("priceRangeV2", {}).get("minVariantPrice", {}).get("amount")
                media = node.get("featuredMedia") or {}
                image = ((media.get("preview") or {}).get("image") or {}).get("url", "")
                products.append(Product(
                    id=str(node["legacyResourceId"]),
                    handle=node["handle"],
                    title=node["title"],
                    stone=metas.get("stone"),
                    metal=metas.get("metal_color"),
                    style=metas.get("style"),
                    price=float(price) if price else None,
                    available=(node.get("totalInventory") or 0) > 0,
                    category=(node.get("category") or {}).get("name"),
                    url=f"https://ruchinewyork.com/products/{node['handle']}",
                    image=image,
                    gid=node["id"],
                ))
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        return products

    def write_related_products(self, recs: dict[str, list[Product]],
                               products_by_id: dict[str, Product]) -> tuple[int, list[str]]:
        """Batched metafieldsSet writes. Returns (written_count, errors)."""
        ns = self.cfg["shopify"]["metafield_namespace"]
        key = self.cfg["shopify"]["metafield_key"]
        batch_size = self.cfg["shopify"]["batch_size"]
        inputs = []
        for pid, rec_products in recs.items():
            if not rec_products:
                continue
            inputs.append({
                "ownerId": products_by_id[pid].gid,
                "namespace": ns,
                "key": key,
                "type": "list.product_reference",
                "value": json.dumps([r.gid for r in rec_products]),
            })
        written, errors = 0, []
        for i in range(0, len(inputs), batch_size):
            batch = inputs[i:i + batch_size]
            data = self._gql(METAFIELDS_SET_MUTATION, {"metafields": batch})
            errs = data["metafieldsSet"]["userErrors"]
            if errs:
                errors.extend(f"{e['field']}: {e['message']}" for e in errs)
            written += len(data["metafieldsSet"]["metafields"] or [])
        return written, errors
