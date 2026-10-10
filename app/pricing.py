import math
import re
import time
from decimal import Decimal

from .config import MARKUP_PERCENT, PUBG_ORDER_CATEGORY, ROUND_TO, RUB_ROUND_TO, USD_RUB_RATE, USD_UZS_RATE
from .fazercards import FazerCards

_CACHE_TTL = 600
_cache: dict[str, tuple[float, list[dict]]] = {}

MIN_CUSTOM_UC = 60
MAX_CUSTOM_UC = 40500
MAX_UNITS = 15  # supplier orders one customer order may need

# UC amounts offered in the shop besides the supplier's own packs. Each is only listed if it can be
# assembled exactly from the supplier's packs (so 335 and 2155 UC are skipped: they cannot be).
PRESET_UC = [60, 120, 180, 335, 385, 660, 720, 985, 1045, 1320, 1950, 2155, 2460, 4000, 4510, 5650, 8400, 9900]


def uzs_price(cost_usd: Decimal | str) -> int:
    raw = float(Decimal(str(cost_usd))) * USD_UZS_RATE * (1 + MARKUP_PERCENT / 100)
    return int(math.ceil(raw / ROUND_TO) * ROUND_TO)


def rub_price(cost_usd: Decimal | str) -> int:
    raw = float(Decimal(str(cost_usd))) * USD_RUB_RATE * (1 + MARKUP_PERCENT / 100)
    return int(math.ceil(raw / RUB_ROUND_TO) * RUB_ROUND_TO)


def price_in(cost_usd: Decimal | str, currency: str) -> int:
    return rub_price(cost_usd) if currency == "RUB" else uzs_price(cost_usd)


def _section(offer_id: str) -> str:
    if re.fullmatch(r"\d+_uc", offer_id):
        return "uc"
    if offer_id.startswith("prime"):
        return "prime"
    if offer_id.startswith("elite_pass"):
        return "pass"
    if offer_id.endswith("_wow_coins"):
        return "wow"
    return "packs"


def _sort_key(p: dict):
    oid = p["offer_id"]
    if p["section"] == "uc":
        return (0, p["uc"])
    if p["section"] == "prime":
        m = re.fullmatch(r"prime(_plus)?_(\d+)_months?", oid)
        return (1, 1 if m and m.group(1) else 0, int(m.group(2)) if m else 0)
    if p["section"] == "wow":
        return (4, int(oid.split("_")[0]))
    return (2 if p["section"] == "pass" else 3, float(p["cost_usd"]))


async def pubg_packs(supplier: FazerCards) -> list[dict]:
    """Every PUBG Mobile product the supplier sells, with our so'm price. Cached; stale cache is served on API errors."""
    hit = _cache.get("pubg")
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        data = await supplier.offers(PUBG_ORDER_CATEGORY)
    except Exception:
        if hit:
            return hit[1]
        raise
    packs = []
    for o in data.get("offers", []):
        section = _section(o["offer_id"])
        m = re.fullmatch(r"(\d+)_uc", o["offer_id"])
        uc = int(m.group(1)) if m else None
        price = uzs_price(o["price_usd"])
        packs.append(
            {
                "offer_id": o["offer_id"],
                "section": section,
                "name": o["name"],
                "uc": uc,
                "cost_usd": o["price_usd"],
                "price": price,
                "price_rub": rub_price(o["price_usd"]),
                "per_uc": round(price / uc, 1) if uc else None,
            }
        )
    packs.sort(key=_sort_key)
    _cache["pubg"] = (time.time(), packs)
    return packs


# ---- exact-amount UC combinations (unbounded knapsack over the supplier's UC packs) ----
_DP_LIMIT = MAX_CUSTOM_UC + 1200
_dp_cache: dict[tuple, list] = {}


def _table(uc_packs: list[dict]) -> list:
    """best[a] = (cheapest cost, fewest packs, last pack index) to reach exactly `a` UC, or None. Built once per price list."""
    sig = tuple((p["uc"], p["cost_usd"]) for p in uc_packs)
    table = _dp_cache.get(sig)
    if table is None:
        cost = [Decimal(p["cost_usd"]) for p in uc_packs]
        table = [None] * (_DP_LIMIT + 1)
        table[0] = (Decimal(0), 0, -1)
        for a in range(1, _DP_LIMIT + 1):
            for i, p in enumerate(uc_packs):
                if a >= p["uc"] and table[a - p["uc"]] is not None:
                    prev = table[a - p["uc"]]
                    cand = (prev[0] + cost[i], prev[1] + 1, i)
                    if table[a] is None or cand[:2] < table[a][:2]:
                        table[a] = cand
        if len(_dp_cache) > 3:
            _dp_cache.clear()
        _dp_cache[sig] = table
    return table


def quote_uc(catalog: list[dict], amount: int) -> dict:
    """Cheapest way to reach exactly `amount` UC from the supplier's UC packs.

    Returns {"ok": True, items, uc, cost_usd, price, units} or {"ok": False, reason, lower, upper}.
    """
    uc_packs = [p for p in catalog if p["section"] == "uc"]
    if not uc_packs:
        return {"ok": False, "reason": "unavailable"}
    if amount < MIN_CUSTOM_UC or amount > MAX_CUSTOM_UC:
        return {"ok": False, "reason": "range", "min": MIN_CUSTOM_UC, "max": MAX_CUSTOM_UC}

    best = _table(uc_packs)
    if best[amount] is None:
        lower = next((a for a in range(amount - 1, 0, -1) if best[a] is not None), None)
        upper = next((a for a in range(amount + 1, _DP_LIMIT + 1) if best[a] is not None), None)
        return {"ok": False, "reason": "not_representable", "lower": lower, "upper": upper}

    counts: dict[int, int] = {}
    a = amount
    while a > 0:
        i = best[a][2]
        counts[i] = counts.get(i, 0) + 1
        a -= uc_packs[i]["uc"]

    units = sum(counts.values())
    if units > MAX_UNITS:
        return {"ok": False, "reason": "too_many", "max_units": MAX_UNITS}

    items = [
        {"offer_id": uc_packs[i]["offer_id"], "name": uc_packs[i]["name"], "uc": uc_packs[i]["uc"], "qty": q}
        for i, q in sorted(counts.items(), key=lambda kv: -uc_packs[kv[0]]["uc"])
    ]
    total = best[amount][0]
    return {
        "ok": True, "items": items, "uc": amount, "cost_usd": str(total),
        "price": uzs_price(total), "price_rub": rub_price(total), "units": units,
    }


def uc_options(catalog: list[dict]) -> list[dict]:
    """Every UC amount the shop lists: the supplier's own packs plus PRESET_UC, where exactly assemblable."""
    amounts = sorted(set(PRESET_UC) | {p["uc"] for p in catalog if p["section"] == "uc"})
    out = []
    for a in amounts:
        q = quote_uc(catalog, a)
        if not q["ok"]:
            continue
        single = len(q["items"]) == 1 and q["items"][0]["qty"] == 1
        out.append(
            {
                "uc": a,
                "price": q["price"],
                "price_rub": q["price_rub"],
                "per_uc": round(q["price"] / a, 1),
                "units": q["units"],
                "offer_id": q["items"][0]["offer_id"] if single else None,
                "items": q["items"],
            }
        )
    return out
