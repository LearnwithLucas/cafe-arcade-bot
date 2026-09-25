"""Pure market maths for TradingCow: tax, quotes, flips, dumps and skill methods.

Nothing here does I/O, so it can be tested with plain dicts.
Wiki conventions: "high" is the instant-buy price, "low" is the instant-sell price.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

BOND_ID = 13190
TAX_CAP = 5_000_000
SHARE = 0.25          # share of hourly volume we assume our offer catches
LIMIT_HOURS = 4


def tax_per_item(item_id: int, price: float) -> int:
    """GE tax: 2% per item, rounded down, capped at 5m. Bonds are exempt."""
    if price <= 0 or item_id == BOND_ID:
        return 0
    return min(TAX_CAP, int(price * 2 // 100))


def net_price(item_id: int, price: float) -> float:
    return price - tax_per_item(item_id, price)


def _avg2(a, b) -> float:
    a = a or 0
    b = b or 0
    if a > 0 and b > 0:
        return (a + b) / 2
    return a if a > 0 else b


@dataclass
class Quote:
    item_id: int
    inst_buy: int
    inst_sell: int
    p_buy: int
    p_sell: int
    net: float
    gross: float
    vol_buy_side: float
    vol_sell_side: float
    age_min: float
    flags: list = field(default_factory=list)   # list of (key, label)

    @property
    def risk(self) -> str:
        keys = {k for k, _ in self.flags}
        if keys & {"stale", "illiquid", "tax"}:
            return "high"
        if keys & {"dip", "spike"}:
            return "medium"
        return "low"

    def classify(self) -> str:
        keys = {k for k, _ in self.flags}
        if "tax" in keys:
            return "Margin disappears after tax"
        if self.net <= 0:
            return "No margin right now"
        if "stale" in keys:
            return "Outdated public data"
        if "illiquid" in keys:
            return "Too illiquid"
        if "dip" in keys:
            return "Temporary dip"
        if "spike" in keys:
            return "Price spike"
        return "Genuine spread"


def volumes(item_id: int, h1: dict, d1: dict) -> tuple[float, float]:
    """Hourly volume on the buy side (people insta-selling) and the sell side."""
    h = h1.get(str(item_id)) or h1.get(item_id) or {}
    d = d1.get(str(item_id)) or d1.get(item_id) or {}

    def pick(hv, dv):
        a = hv if hv and hv > 0 else 0
        b = dv / 24 if dv and dv > 0 else 0
        return (a + b) / 2 if a and b else (a or b)

    return pick(h.get("lowPriceVolume"), d.get("lowPriceVolume")), pick(h.get("highPriceVolume"), d.get("highPriceVolume"))


def quote(item_id: int, latest: dict, h1: dict, d1: dict, now: float | None = None) -> Quote | None:
    now = now or time.time()
    L = latest.get(str(item_id)) or latest.get(item_id)
    if not L or not L.get("high") or not L.get("low"):
        return None
    h = h1.get(str(item_id)) or h1.get(item_id) or {}
    d = d1.get(str(item_id)) or d1.get(item_id) or {}
    p_buy = round(_avg2(L["low"], h.get("avgLowPrice")))
    p_sell = round(_avg2(L["high"], h.get("avgHighPrice")))
    net = net_price(item_id, p_sell) - p_buy
    gross = p_sell - p_buy
    vb, vs = volumes(item_id, h1, d1)
    age = (now - min(L.get("highTime") or 0, L.get("lowTime") or 0)) / 60
    flags = []
    if age > 60:
        flags.append(("stale", "stale data"))
    if d.get("avgLowPrice") and L["low"] < d["avgLowPrice"] * 0.95:
        flags.append(("dip", "dip vs 24h"))
    if d.get("avgHighPrice") and L["high"] > d["avgHighPrice"] * 1.05:
        flags.append(("spike", "spike vs 24h"))
    if min(vb, vs) < 50:
        flags.append(("illiquid", "illiquid"))
    if gross > 0 and net <= 0:
        flags.append(("tax", "tax-killed"))
    return Quote(item_id, L["high"], L["low"], p_buy, p_sell, net, gross, vb, vs, age, flags)


@dataclass
class Flip:
    item_id: int
    name: str
    q: Quote
    qty: int
    profit: float
    hours: float
    score: float


def flips(mapping: dict, latest: dict, h1: dict, d1: dict, *, cash: int, slots: int, max_hours: float,
          min_profit: int, f2p: bool, max_risk: str, min_margin: int = 5, min_roi: float = 0.01) -> list[Flip]:
    """Best patient flips: buy near the instant-sell price, sell near the instant-buy price.

    A flip needs at least min_margin gp and min_roi profit per item after tax, so one price
    tick against you cannot wipe it out.
    """
    per_slot = cash / max(1, slots)
    risk_rank = {"low": 0, "medium": 1, "high": 2}
    out = []
    for item_id, m in mapping.items():
        if f2p and m.get("members"):
            continue
        q = quote(item_id, latest, h1, d1)
        if not q or q.net <= 0 or risk_rank[q.risk] > risk_rank[max_risk]:
            continue
        if not (q.vol_buy_side > 0 and q.vol_sell_side > 0) or q.p_buy <= 0:
            continue
        if q.net < min_margin or q.net / q.p_buy < min_roi:
            continue
        per_unit_h = 1 / SHARE * (1 / q.vol_buy_side + 1 / q.vol_sell_side)
        limit = m.get("limit") or 10**9
        qty = int(min(limit, per_slot / q.p_buy, max_hours / per_unit_h))
        if qty < 1:
            continue
        profit = qty * q.net
        if profit < min_profit:
            continue
        hours = qty * per_unit_h
        score = profit / max(hours, 0.25) * (0.5 ** len(q.flags))
        out.append(Flip(item_id, m["name"], q, qty, profit, hours, score))
    out.sort(key=lambda f: f.score, reverse=True)
    return out


# ---------------------------------------------------------------- dumps

@dataclass
class Dump:
    item_id: int
    name: str
    base_low: float        # volume-weighted average instant-sell price over the window
    base_high: float
    cur_low: int
    cur_high: int
    drop_pct: float        # how far the current instant-sell price is under the baseline
    hour_drop_pct: float   # same, using the last full hour's average
    sell_vol_ratio: float  # last hour's insta-sell volume vs the window's hourly average
    sudden: bool           # price was near baseline 24h ago
    upside_each: float     # net profit per item if it returns to the baseline
    daily_gp_volume: float
    limit: int

    @property
    def upside_pct(self) -> float:
        return self.upside_each / self.cur_low * 100 if self.cur_low else 0


def find_dumps(mapping: dict, latest: dict, history: dict, *, now: float | None = None, min_drop: float = 0.10,
               min_daily_gp: float = 5_000_000, min_price: int = 50, f2p: bool = False,
               window_hours: int = 168, min_sell_ratio: float = 1.5) -> list[Dump]:
    """history: {item_id: [(ts, avg_high, avg_low, high_vol, low_vol), ...]} hourly rows, any order.

    A dump is an item whose current instant-sell price sits well under its volume-weighted
    average for the window, confirmed by the last full hour (so one stray trade does not count)
    and by heavy selling: the last hour's insta-sell volume must be min_sell_ratio times normal.
    A lower price on thin trading is not a dump.
    """
    now = now or time.time()
    start = now - window_hours * 3600
    recent_cut = now - 6 * 3600
    out = []
    for item_id, rows in history.items():
        m = mapping.get(item_id)
        if not m or (f2p and m.get("members")):
            continue
        L = latest.get(str(item_id)) or latest.get(item_id)
        if not L or not L.get("low") or not L.get("high"):
            continue
        if now - (L.get("lowTime") or 0) > 45 * 60:
            continue
        base = [r for r in rows if start <= r[0] < recent_cut and r[2] and r[4]]
        if len(base) < min(24, window_hours // 2):
            continue
        vol_low = sum(r[4] for r in base)
        base_low = sum(r[2] * r[4] for r in base) / vol_low
        hb = [r for r in base if r[1] and r[3]]
        base_high = sum(r[1] * r[3] for r in hb) / sum(r[3] for r in hb) if hb else base_low
        hours_span = max(1.0, (recent_cut - max(start, min(r[0] for r in base))) / 3600)
        daily_units = (vol_low + sum(r[3] or 0 for r in base)) / hours_span * 24
        daily_gp = daily_units * base_low
        cur_low = L["low"]
        if cur_low < min_price or daily_gp < min_daily_gp:
            continue
        drop = 1 - cur_low / base_low
        if drop < min_drop:
            continue
        last = max((r for r in rows if r[0] >= now - 3 * 3600 and r[2]), key=lambda r: r[0], default=None)
        if not last:
            continue
        hour_drop = 1 - last[2] / base_low
        if hour_drop < min_drop * 0.6:
            continue
        hourly_avg_sell = vol_low / hours_span
        ratio = (last[4] or 0) / hourly_avg_sell if hourly_avg_sell else 0
        if ratio < min_sell_ratio:
            continue
        day_ago = [r for r in rows if now - 30 * 3600 <= r[0] <= now - 20 * 3600 and r[2]]
        sudden = bool(day_ago) and min(abs(1 - r[2] / base_low) for r in day_ago) < 0.05
        target = (base_low + base_high) / 2
        upside = net_price(item_id, target) - cur_low
        out.append(Dump(item_id, m["name"], base_low, base_high, cur_low, L["high"], drop * 100, hour_drop * 100,
                        ratio, sudden, upside, daily_gp, m.get("limit") or 0))
    out.sort(key=lambda d: (d.sudden, d.drop_pct), reverse=True)
    return out


# ---------------------------------------------------------------- skill methods

def _m(skill, name, lvl, xp, inputs, outputs, members=False, success=100):
    return {"skill": skill, "name": name, "lvl": lvl, "xp": xp, "inputs": inputs, "outputs": outputs,
            "members": members, "success": success}


METHODS: list[dict] = []
for out, lvl, xp, gem in [
    ("Gold ring", 5, 15, None), ("Gold necklace", 6, 20, None), ("Gold amulet (u)", 8, 30, None),
    ("Sapphire ring", 20, 40, "Sapphire"), ("Sapphire necklace", 22, 55, "Sapphire"), ("Sapphire amulet (u)", 24, 65, "Sapphire"),
    ("Emerald ring", 27, 55, "Emerald"), ("Emerald necklace", 29, 60, "Emerald"), ("Emerald amulet (u)", 31, 70, "Emerald"),
    ("Ruby ring", 34, 70, "Ruby"), ("Ruby necklace", 40, 75, "Ruby"), ("Ruby amulet (u)", 50, 85, "Ruby"),
    ("Diamond ring", 43, 85, "Diamond"), ("Diamond necklace", 56, 90, "Diamond"), ("Diamond amulet (u)", 70, 100, "Diamond"),
]:
    METHODS.append(_m("Crafting", out, lvl, xp, [("Gold bar", 1)] + ([(gem, 1)] if gem else []), [(out, 1)]))
for g, lvl, xp in [("Sapphire", 20, 50), ("Emerald", 27, 67.5), ("Ruby", 34, 85), ("Diamond", 43, 107.5), ("Dragonstone", 55, 137.5)]:
    METHODS.append(_m("Crafting", "Cut " + g.lower(), lvl, xp, [("Uncut " + g.lower(), 1)], [(g, 1)]))
METHODS += [
    _m("Crafting", "Molten glass", 1, 20, [("Soda ash", 1), ("Bucket of sand", 1)], [("Molten glass", 1)]),
    _m("Crafting", "Leather gloves", 1, 13.8, [("Leather", 1)], [("Leather gloves", 1)]),
    _m("Crafting", "Leather body", 14, 25, [("Leather", 1)], [("Leather body", 1)]),
    _m("Crafting", "Unpowered orb", 46, 52.5, [("Molten glass", 1)], [("Unpowered orb", 1)]),
    _m("Crafting", "Air battlestaff", 66, 137.5, [("Battlestaff", 1), ("Air orb", 1)], [("Air battlestaff", 1)]),
    _m("Crafting", "Green d'hide body", 63, 186, [("Green dragon leather", 3)], [("Green d'hide body", 1)]),
    _m("Smithing", "Bronze bar", 1, 6.2, [("Copper ore", 1), ("Tin ore", 1)], [("Bronze bar", 1)]),
    _m("Smithing", "Iron bar (50% success)", 15, 12.5, [("Iron ore", 1)], [("Iron bar", 1)], success=50),
    _m("Smithing", "Silver bar", 20, 13.7, [("Silver ore", 1)], [("Silver bar", 1)]),
    _m("Smithing", "Steel bar", 30, 17.5, [("Iron ore", 1), ("Coal", 2)], [("Steel bar", 1)]),
    _m("Smithing", "Gold bar", 40, 22.5, [("Gold ore", 1)], [("Gold bar", 1)]),
    _m("Smithing", "Mithril bar", 50, 30, [("Mithril ore", 1), ("Coal", 4)], [("Mithril bar", 1)]),
    _m("Smithing", "Adamantite bar", 70, 37.5, [("Adamantite ore", 1), ("Coal", 6)], [("Adamantite bar", 1)]),
    _m("Smithing", "Runite bar", 85, 50, [("Runite ore", 1), ("Coal", 8)], [("Runite bar", 1)]),
    _m("Smithing", "Cannonballs", 35, 25.6, [("Steel bar", 1)], [("Cannonball", 4)], members=True),
]
for bar, item, lvl, xp in [("Bronze", "Bronze", 18, 62.5), ("Iron", "Iron", 33, 125), ("Steel", "Steel", 48, 187.5),
                           ("Mithril", "Mithril", 68, 250), ("Adamantite", "Adamant", 88, 312.5), ("Runite", "Rune", 99, 375)]:
    METHODS.append(_m("Smithing", item + " platebody", lvl, xp, [(bar + " bar", 5)], [(item + " platebody", 1)]))
for f, lvl, xp in [("Trout", 15, 70), ("Salmon", 25, 90), ("Tuna", 30, 100), ("Lobster", 40, 120), ("Swordfish", 45, 140),
                   ("Monkfish", 62, 150), ("Shark", 80, 210), ("Anglerfish", 84, 230)]:
    METHODS.append(_m("Cooking", f, lvl, xp, [("Raw " + f.lower(), 1)], [(f, 1)]))
METHODS += [
    _m("Cooking", "Cooked karambwan", 30, 190, [("Raw karambwan", 1)], [("Cooked karambwan", 1)]),
    _m("Cooking", "Jug of wine", 35, 200, [("Grapes", 1), ("Jug of water", 1)], [("Jug of wine", 1)]),
]
for w, lvl, xp in [("Maple", 55, 58.3), ("Yew", 70, 75), ("Magic", 85, 91.5)]:
    METHODS.append(_m("Fletching", w + " longbow (u)", lvl, xp, [(w + " logs", 1)], [(w + " longbow (u)", 1)], members=True))
    METHODS.append(_m("Fletching", w + " longbow (string)", lvl, xp, [(w + " longbow (u)", 1), ("Bow string", 1)], [(w + " longbow", 1)], members=True))
for l, lvl, xp in [("Logs", 1, 40), ("Oak logs", 15, 60), ("Willow logs", 30, 90), ("Maple logs", 45, 135), ("Yew logs", 60, 202.5), ("Magic logs", 75, 303.8)]:
    METHODS.append(_m("Firemaking", "Burn " + l.lower(), lvl, xp, [(l, 1)], []))
METHODS += [
    _m("Herblore", "Prayer potion(3)", 38, 87.5, [("Ranarr potion (unf)", 1), ("Snape grass", 1)], [("Prayer potion(3)", 1)], members=True),
    _m("Herblore", "Super restore(3)", 63, 142.5, [("Snapdragon potion (unf)", 1), ("Red spiders' eggs", 1)], [("Super restore(3)", 1)], members=True),
    _m("Herblore", "Saradomin brew(3)", 81, 180, [("Toadflax potion (unf)", 1), ("Crushed nest", 1)], [("Saradomin brew(3)", 1)], members=True),
    _m("Prayer", "Bury big bones", 1, 15, [("Big bones", 1)], []),
    _m("Prayer", "Bury dragon bones", 1, 72, [("Dragon bones", 1)], []),
    _m("Prayer", "Dragon bones on gilded altar", 1, 252, [("Dragon bones", 1)], [], members=True),
    _m("Construction", "Oak larder", 33, 480, [("Oak plank", 8)], [], members=True),
    _m("Construction", "Mahogany table", 52, 840, [("Mahogany plank", 6)], [], members=True),
    _m("Construction", "Oak dungeon door", 74, 600, [("Oak plank", 10)], [], members=True),
]
SKILLS = ["Crafting", "Smithing", "Cooking", "Fletching", "Firemaking", "Herblore", "Prayer", "Construction"]


def xp_for_level(level: int) -> int:
    pts = 0
    for i in range(1, level):
        pts += math.floor(i + 300 * 2 ** (i / 7))
    return pts // 4


@dataclass
class MethodResult:
    method: dict
    xp: float
    profit: float
    cost: float
    members: bool
    missing: list

    @property
    def per_xp(self) -> float | None:
        return self.profit / self.xp if self.xp > 0 else None


def eval_method(m: dict, by_name: dict, mapping: dict, latest: dict, h1: dict, d1: dict, instant: bool = False) -> MethodResult:
    success = m["success"] / 100
    cost = value = 0.0
    missing = []
    members = m["members"]
    for name, qty in m["inputs"]:
        iid = by_name.get(name.lower())
        q = quote(iid, latest, h1, d1) if iid else None
        if not q:
            missing.append(name)
            continue
        members = members or bool(mapping[iid].get("members"))
        cost += (q.inst_buy if instant else q.p_buy) * qty
    for name, qty in m["outputs"]:
        iid = by_name.get(name.lower())
        q = quote(iid, latest, h1, d1) if iid else None
        if not q:
            missing.append(name)
            continue
        members = members or bool(mapping[iid].get("members"))
        value += net_price(iid, q.inst_sell if instant else q.p_sell) * qty * success
    return MethodResult(m, m["xp"] * success, value - cost, cost, members, missing)
