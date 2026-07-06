"""Rule-based recommendation scoring.

Priority: same stone > same metal > same style family > same price point.
Weights are configured in config.yaml so that each tier strictly outranks
the sum of all lower tiers.
"""
from __future__ import annotations

import csv
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Product:
    id: str                    # numeric product id as string
    handle: str
    title: str
    stone: Optional[str] = None
    metal: Optional[str] = None
    style: Optional[str] = None        # product-type style (hoops, fashion-ring...)
    category: Optional[str] = None     # Earrings / Rings / Necklaces / Bracelets
    price: Optional[float] = None      # min variant price
    available: bool = True
    url: str = ""
    image: str = ""
    family: Optional[str] = None       # derived style family (arles, lyre...)
    gid: str = ""

    def __post_init__(self):
        if not self.gid:
            self.gid = f"gid://shopify/Product/{self.id}"


def load_family_overrides(path: str) -> dict[str, str]:
    """CSV with columns handle,family. Empty family = force no family."""
    overrides = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("handle"):
                    overrides[row["handle"].strip()] = (row.get("family") or "").strip().lower()
    return overrides


def derive_families(products: list[Product], overrides: dict[str, str],
                    generic_first_words: list[str]) -> list[dict]:
    """Assign style family from title first word, with overrides.

    Returns review rows for families that look ambiguous (generic first word,
    or family with a single member), for human confirmation.
    """
    generic = {w.lower() for w in generic_first_words}
    first_words = Counter()
    for p in products:
        fw = p.title.split()[0].lower() if p.title else ""
        first_words[fw] += 1

    review = []
    for p in products:
        if p.handle in overrides:
            p.family = overrides[p.handle] or None
            continue
        fw = p.title.split()[0].lower() if p.title else ""
        if not fw:
            continue
        if fw in generic:
            p.family = None
            review.append({"handle": p.handle, "title": p.title,
                           "suggested_family": "", "reason": f"generic first word '{fw}'"})
        elif first_words[fw] == 1:
            p.family = fw
            review.append({"handle": p.handle, "title": p.title,
                           "suggested_family": fw, "reason": "only product in this family"})
        else:
            p.family = fw
    return review


GENERIC_CATEGORIES = {"", "jewelry", "uncategorized", "gift cards"}


def derive_categories(products: list[Product], cfg: dict) -> int:
    """Fill Product.category from explicit taxonomy when specific, else title
    keywords. Returns count of products left uncategorized."""
    keywords = cfg["category_keywords"]
    missing = 0
    for p in products:
        explicit = (p.category or "").strip()
        if explicit and explicit.lower() not in GENERIC_CATEGORIES:
            p.category = explicit
            continue
        p.category = None
        words = [w.strip(",.()").lower() for w in p.title.split()]
        for cat, keys in keywords.items():
            if any(w in keys for w in reversed(words)):  # last words most reliable
                p.category = cat
                break
        if p.category is None:
            missing += 1
    return missing


def score_pair(target: Product, cand: Product, cfg: dict) -> Optional[float]:
    """Score candidate against target. None = hard-excluded."""
    w = cfg["weights"]
    score = 0.0
    stone = bool(target.stone and cand.stone and target.stone == cand.stone)
    metal = bool(target.metal and cand.metal and target.metal == cand.metal)
    if stone and metal:
        score += w["stone_and_metal"]
    elif stone:
        score += w["stone_only"]
    elif metal:
        score += w["metal_only"]
    if target.category and cand.category and target.category == cand.category:
        score += w["category"]
    family_match = bool(target.family and cand.family and target.family == cand.family)
    if family_match:
        score += w["style_family"]
    if target.price and cand.price:
        ratio = max(target.price, cand.price) / max(min(target.price, cand.price), 0.01)
        if ratio > cfg["price_ratio_cap"]:
            # same-family pieces may bypass the cap ("complete the look"),
            # unless the gap is extreme
            exempt = cfg.get("price_cap_family_exempt", True) and family_match \
                and ratio <= cfg.get("price_ratio_cap_family", 6.0)
            if not exempt:
                return None  # never show a $70k piece next to a $7k piece
        if ratio <= cfg["price_band_ratio"]:
            score += w["price_band"]
        elif ratio <= cfg["price_band_near_ratio"]:
            score += w["price_band_near"]
    return score


def build_recommendations(products: list[Product], cfg: dict) -> dict[str, list[Product]]:
    """Return {product_id: [recommended Products]} using the weighted rules."""
    n = cfg["recommendations_per_product"]
    candidates = [p for p in products if p.available or not cfg["exclude_zero_inventory"]]
    recs: dict[str, list[Product]] = {}
    for target in products:
        scored = []
        for cand in candidates:
            if cand.id == target.id:
                continue
            s = score_pair(target, cand, cfg)
            if s is None or s <= 0:
                continue
            # tie-breakers: closer price first, then title for determinism
            price_gap = abs((cand.price or 0) - (target.price or 0))
            scored.append((-s, price_gap, cand.title, cand))
        scored.sort(key=lambda x: (x[0], x[1], x[2]))
        recs[target.id] = [x[3] for x in scored[:n]]
    return recs
