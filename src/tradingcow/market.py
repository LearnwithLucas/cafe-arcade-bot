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


NATURE_RUNE_ID = 561
ALCHS_PER_HOUR = 1200   # about the most a player casts in an hour


@dataclass
class Alch:
    item_id: int
    name: str
    buy: int            # price paid per item
    alch: int           # high alch value
    nature: int         # nature rune price
    profit: float       # per cast, after the nature rune
    per_hour: float     # casts per hour we can supply (buy limit and volume)
    limit: int

    @property
    def profit_hour(self) -> float:
        return self.profit * self.per_hour


def alchs(mapping: dict, latest: dict, h1: dict, d1: dict, *, f2p: bool, members_only: bool = False,
          instant: bool = True, min_profit: int = 1) -> list[Alch]:
    """Best items to buy and high alch. Instant buys at the insta-buy price; patient buys near the insta-sell price."""
    nat = latest.get(str(NATURE_RUNE_ID)) or latest.get(NATURE_RUNE_ID) or {}
    nature = nat.get("high") or nat.get("low") or 0
    if not nature:
        return []
    out = []
    for iid, m in mapping.items():
        ha = m.get("highalch") or 0
        if not ha or (f2p and m.get("members")) or (members_only and not m.get("members")):
            continue
        L = latest.get(str(iid)) or latest.get(iid) or {}
        buy = L.get("high") if instant else (L.get("low") or L.get("high"))
        if not buy:
            continue
        profit = ha - buy - nature
        if profit < min_profit:
            continue
        vb, vs = volumes(iid, h1, d1)
        supply = (vs if instant else vb) * SHARE        # hourly trades we can realistically catch
        limit = m.get("limit") or 0
        caps = [ALCHS_PER_HOUR, supply]
        if limit:
            caps.append(limit / LIMIT_HOURS)
        per_hour = max(0.0, min(caps))
        if per_hour < 1:
            continue
        out.append(Alch(iid, m["name"], int(buy), int(ha), int(nature), profit, per_hour, int(limit)))
    return sorted(out, key=lambda a: a.profit_hour, reverse=True)


def flips(mapping: dict, latest: dict, h1: dict, d1: dict, *, cash: int, slots: int, max_hours: float,
          min_profit: int, f2p: bool, max_risk: str, min_margin: int = 5, min_roi: float = 0.01, members_only: bool = False) -> list[Flip]:
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
        if members_only and not m.get("members"):
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

def _m(skill, name, lvl, xp, inputs, outputs, members=False, success=100, rate=1200):
    """One skilling action. rate = rough actions per hour for a normal player (banking included).
    An item name may list alternatives with "|" when the Wiki spelling is uncertain."""
    return {"skill": skill, "name": name, "lvl": lvl, "xp": xp, "inputs": inputs, "outputs": outputs,
            "members": members, "success": success, "rate": rate}


METHODS: list[dict] = []
M = METHODS.append

# ---------------- Crafting
for out, lvl, xp, gem, mem in [
    ("Gold ring", 5, 15, None, False), ("Gold necklace", 6, 20, None, False), ("Gold amulet (u)", 8, 30, None, False),
    ("Sapphire ring", 20, 40, "Sapphire", False), ("Sapphire necklace", 22, 55, "Sapphire", False), ("Sapphire amulet (u)", 24, 65, "Sapphire", False),
    ("Emerald ring", 27, 55, "Emerald", False), ("Emerald necklace", 29, 60, "Emerald", False), ("Emerald amulet (u)", 31, 70, "Emerald", False),
    ("Ruby ring", 34, 70, "Ruby", False), ("Ruby necklace", 40, 75, "Ruby", False), ("Ruby amulet (u)", 50, 85, "Ruby", False),
    ("Diamond ring", 43, 85, "Diamond", False), ("Diamond necklace", 56, 90, "Diamond", False), ("Diamond amulet (u)", 70, 100, "Diamond", False),
    ("Dragonstone ring", 55, 100, "Dragonstone", True), ("Dragon necklace", 72, 105, "Dragonstone", True),
    ("Dragonstone amulet (u)", 80, 150, "Dragonstone", True),
]:
    M(_m("Crafting", out, lvl, xp, [("Gold bar", 1)] + ([(gem, 1)] if gem else []), [(out, 1)], members=mem, rate=1300))
for g, lvl, xp in [("Sapphire", 1, 4), ("Emerald", 1, 4), ("Ruby", 1, 4), ("Diamond", 1, 4), ("Dragonstone", 1, 4)]:
    M(_m("Crafting", f"String {g.lower()} amulet", lvl, xp, [(f"{g} amulet (u)", 1), ("Ball of wool", 1)], [(f"{g} amulet", 1)],
         members=g == "Dragonstone", rate=2500))
for g, lvl, xp, mem in [("Opal", 1, 15, True), ("Jade", 13, 20, True), ("Red topaz", 16, 25, True), ("Sapphire", 20, 50, False),
                        ("Emerald", 27, 67.5, False), ("Ruby", 34, 85, False), ("Diamond", 43, 107.5, False),
                        ("Dragonstone", 55, 137.5, True), ("Onyx", 67, 167.5, True)]:
    M(_m("Crafting", "Cut " + g.lower(), lvl, xp, [("Uncut " + g.lower(), 1)], [(g, 1)], members=mem, rate=2700))
M(_m("Crafting", "Molten glass", 1, 20, [("Soda ash", 1), ("Bucket of sand", 1)], [("Molten glass", 1)], rate=1700))
for out, lvl, xp in [("Vial", 33, 35), ("Unpowered orb", 46, 52.5), ("Lantern lens", 49, 55), ("Empty light orb", 87, 70)]:
    M(_m("Crafting", out + " (glassblowing)", lvl, xp, [("Molten glass", 1)], [(out, 1)], members=True, rate=1600))
for out, lvl, xp in [("Leather gloves", 1, 13.8), ("Leather boots", 7, 16.25), ("Leather cowl", 9, 18.5),
                     ("Leather vambraces", 11, 22), ("Leather body", 14, 25), ("Leather chaps", 18, 27)]:
    M(_m("Crafting", out, lvl, xp, [("Leather", 1)], [(out, 1)], rate=1500))
M(_m("Crafting", "Hardleather body", 28, 35, [("Hard leather", 1)], [("Hardleather body", 1)], rate=1500))
for colour, base in [("Green", 57), ("Blue", 66), ("Red", 73), ("Black", 79)]:
    leather = f"{colour} dragon leather"
    vxp = {"Green": 62, "Blue": 70, "Red": 78, "Black": 86}[colour]
    M(_m("Crafting", f"{colour} d'hide vambraces", base, vxp, [(leather, 1)], [(f"{colour} d'hide vambraces|{colour} d'hide vambs", 1)],
         members=True, rate=1650))
    M(_m("Crafting", f"{colour} d'hide chaps", base + {"Green": 3, "Blue": 2, "Red": 2, "Black": 3}[colour], vxp * 2,
         [(leather, 2)], [(f"{colour} d'hide chaps", 1)], members=True, rate=1400))
    M(_m("Crafting", f"{colour} d'hide body", base + {"Green": 6, "Blue": 5, "Red": 4, "Black": 5}[colour], vxp * 3,
         [(leather, 3)], [(f"{colour} d'hide body", 1)], members=True, rate=1250))
for orb, lvl, xp in [("Water", 54, 100), ("Earth", 58, 112.5), ("Fire", 62, 125), ("Air", 66, 137.5)]:
    M(_m("Crafting", f"{orb} battlestaff", lvl, xp, [("Battlestaff", 1), (f"{orb} orb", 1)], [(f"{orb} battlestaff", 1)],
         members=True, rate=2500))

# ---------------- Smithing
METHODS += [
    _m("Smithing", "Bronze bar", 1, 6.2, [("Copper ore", 1), ("Tin ore", 1)], [("Bronze bar", 1)], rate=1000),
    _m("Smithing", "Iron bar (50% success)", 15, 12.5, [("Iron ore", 1)], [("Iron bar", 1)], success=50, rate=1000),
    _m("Smithing", "Silver bar", 20, 13.7, [("Silver ore", 1)], [("Silver bar", 1)], rate=1000),
    _m("Smithing", "Steel bar", 30, 17.5, [("Iron ore", 1), ("Coal", 2)], [("Steel bar", 1)], rate=1000),
    _m("Smithing", "Gold bar", 40, 22.5, [("Gold ore", 1)], [("Gold bar", 1)], rate=1300),
    _m("Smithing", "Mithril bar", 50, 30, [("Mithril ore", 1), ("Coal", 4)], [("Mithril bar", 1)], rate=900),
    _m("Smithing", "Adamantite bar", 70, 37.5, [("Adamantite ore", 1), ("Coal", 6)], [("Adamantite bar", 1)], rate=800),
    _m("Smithing", "Runite bar", 85, 50, [("Runite ore", 1), ("Coal", 8)], [("Runite bar", 1)], rate=700),
    _m("Smithing", "Cannonballs", 35, 25.6, [("Steel bar", 1)], [("Cannonball", 4)], members=True, rate=650),
]
for bar, item, lvl, xp in [("Bronze", "Bronze", 18, 62.5), ("Iron", "Iron", 33, 125), ("Steel", "Steel", 48, 187.5),
                           ("Mithril", "Mithril", 68, 250), ("Adamantite", "Adamant", 88, 312.5), ("Runite", "Rune", 99, 375)]:
    M(_m("Smithing", item + " platebody", lvl, xp, [(bar + " bar", 5)], [(item + " platebody", 1)], rate=330))
for bar, item, lvl, xp in [("Bronze", "Bronze", 4, 12.5), ("Iron", "Iron", 19, 25), ("Steel", "Steel", 34, 37.5),
                           ("Mithril", "Mithril", 54, 50), ("Adamantite", "Adamant", 74, 62.5), ("Runite", "Rune", 89, 75)]:
    M(_m("Smithing", f"{item} dart tips", lvl, xp, [(bar + " bar", 1)], [(f"{item} dart tip", 10)], members=True, rate=1000))

# ---------------- Cooking
for f, lvl, xp in [("Trout", 15, 70), ("Salmon", 25, 90), ("Tuna", 30, 100), ("Lobster", 40, 120), ("Swordfish", 45, 140),
                   ("Monkfish", 62, 150), ("Shark", 80, 210), ("Anglerfish", 84, 230)]:
    M(_m("Cooking", f, lvl, xp, [("Raw " + f.lower(), 1)], [(f, 1)], rate=1300))
METHODS += [
    _m("Cooking", "Cooked karambwan", 30, 190, [("Raw karambwan", 1)], [("Cooked karambwan", 1)], rate=1300),
    _m("Cooking", "Jug of wine", 35, 200, [("Grapes", 1), ("Jug of water", 1)], [("Jug of wine", 1)], members=True, rate=2400),
]

# ---------------- Fletching (members skill)
LOGS = [("Logs", "", 1, 5, 10), ("Oak logs", "Oak ", 15, 20, 25), ("Willow logs", "Willow ", 30, 35, 40),
        ("Maple logs", "Maple ", 45, 50, 55), ("Yew logs", "Yew ", 60, 65, 70), ("Magic logs", "Magic ", 75, 80, 85)]
SHAFTS = {"Logs": (15, 5), "Oak logs": (30, 10), "Willow logs": (45, 15), "Maple logs": (60, 20), "Yew logs": (75, 25), "Magic logs": (90, 30)}
BOW_XP = {"": (5, 10), "Oak ": (16.5, 25), "Willow ": (33.3, 41.5), "Maple ": (50, 58.3), "Yew ": (67.5, 75), "Magic ": (83.3, 91.5)}
for logs, w, shaft_lvl, sb_lvl, lb_lvl in LOGS:
    n_shafts, sxp = SHAFTS[logs]
    M(_m("Fletching", f"Arrow shafts from {logs.lower()}", shaft_lvl, sxp, [(logs, 1)], [("Arrow shaft", n_shafts)], members=True, rate=1700))
    sbx, lbx = BOW_XP[w]
    for kind, lvl, xp in [("shortbow", sb_lvl, sbx), ("longbow", lb_lvl, lbx)]:
        strung = (w + kind).capitalize() if w else kind.capitalize()
        M(_m("Fletching", f"{strung} (u)", lvl, xp, [(logs, 1)], [(f"{strung} (u)", 1)], members=True, rate=1700))
        M(_m("Fletching", f"String {strung.lower()}", lvl, xp, [(f"{strung} (u)", 1), ("Bow string", 1)], [(strung, 1)],
             members=True, rate=2400))
M(_m("Fletching", "Headless arrows (15)", 1, 15, [("Arrow shaft", 15), ("Feather", 15)], [("Headless arrow", 15)], members=True, rate=750))
for tip, lvl, xp in [("Bronze", 1, 1.3), ("Iron", 15, 2.5), ("Steel", 30, 5), ("Mithril", 45, 7.5), ("Adamant", 60, 10),
                     ("Rune", 75, 12.5), ("Amethyst", 82, 13.5), ("Dragon", 90, 15)]:
    M(_m("Fletching", f"{tip} arrows (15)", lvl, xp * 15, [("Headless arrow", 15), (f"{tip} arrowtips", 15)], [(f"{tip} arrow", 15)],
         members=True, rate=750))
for tip, lvl, xp in [("Bronze", 10, 1.8), ("Iron", 22, 3.8), ("Steel", 37, 7.5), ("Mithril", 52, 11.2), ("Adamant", 67, 15),
                     ("Rune", 81, 18.8), ("Amethyst", 90, 21), ("Dragon", 95, 25)]:
    M(_m("Fletching", f"{tip} darts (10)", lvl, xp * 10, [(f"{tip} dart tip", 10), ("Feather", 10)], [(f"{tip} dart", 10)],
         members=True, rate=1500))
for unf, out, lvl, xp in [("Bronze bolts (unf)", "Bronze bolts", 9, 0.5), ("Iron bolts (unf)", "Iron bolts", 39, 1.5),
                          ("Steel bolts (unf)", "Steel bolts", 46, 3.5), ("Mithril bolts (unf)", "Mithril bolts", 54, 5),
                          ("Adamant bolts(unf)|Adamant bolts (unf)", "Adamant bolts", 61, 7),
                          ("Runite bolts (unf)", "Runite bolts", 69, 10), ("Dragon bolts (unf)", "Dragon bolts", 84, 12)]:
    M(_m("Fletching", f"{out} (10)", lvl, xp * 10, [(unf, 10), ("Feather", 10)], [(out, 10)], members=True, rate=1500))

# ---------------- Firemaking
for l, lvl, xp in [("Logs", 1, 40), ("Oak logs", 15, 60), ("Willow logs", 30, 90), ("Maple logs", 45, 135),
                   ("Yew logs", 60, 202.5), ("Magic logs", 75, 303.8), ("Redwood logs", 90, 350)]:
    M(_m("Firemaking", "Burn " + l.lower(), lvl, xp, [(l, 1)], [], members=l == "Redwood logs", rate=1200))

# ---------------- Herblore (members skill)
HERBS = [("Guam leaf", "Guam", 3, 3, 2.5), ("Marrentill", "Marrentill", 5, 5, 3.8), ("Tarromin", "Tarromin", 11, 12, 5),
         ("Harralander", "Harralander", 20, 22, 6.3), ("Ranarr weed", "Ranarr", 25, 30, 7.5), ("Toadflax", "Toadflax", 30, 34, 8),
         ("Irit leaf", "Irit", 40, 45, 8.8), ("Avantoe", "Avantoe", 48, 50, 10), ("Kwuarm", "Kwuarm", 54, 55, 11.3),
         ("Snapdragon", "Snapdragon", 59, 63, 11.8), ("Cadantine", "Cadantine", 65, 66, 12.5), ("Lantadyme", "Lantadyme", 67, 69, 13.1),
         ("Dwarf weed", "Dwarf weed", 70, 72, 13.8), ("Torstol", "Torstol", 75, 78, 15)]
for herb, short, clean_lvl, unf_lvl, cxp in HERBS:
    M(_m("Herblore", f"Clean grimy {herb.lower()}", clean_lvl, cxp, [(f"Grimy {herb.lower()}", 1)], [(herb, 1)], members=True, rate=4000))
    M(_m("Herblore", f"{short} potion (unf)", unf_lvl, 0, [(herb, 1), ("Vial of water", 1)], [(f"{short} potion (unf)", 1)],
         members=True, rate=2700))
for out, lvl, xp, unf, sec in [
    ("Prayer potion(3)", 38, 87.5, "Ranarr potion (unf)", "Snape grass"),
    ("Super attack(3)", 45, 100, "Irit potion (unf)", "Eye of newt"),
    ("Super energy(3)", 52, 117.5, "Avantoe potion (unf)", "Mort myre fungus"),
    ("Super strength(3)", 55, 125, "Kwuarm potion (unf)", "Limpwurt root"),
    ("Super restore(3)", 63, 142.5, "Snapdragon potion (unf)", "Red spiders' eggs"),
    ("Super defence(3)", 66, 150, "Cadantine potion (unf)", "White berries"),
    ("Antifire potion(3)", 69, 157.5, "Lantadyme potion (unf)", "Dragon scale dust"),
    ("Ranging potion(3)", 72, 162.5, "Dwarf weed potion (unf)", "Wine of zamorak"),
    ("Magic potion(3)", 76, 172.5, "Lantadyme potion (unf)", "Potato cactus"),
    ("Saradomin brew(3)", 81, 180, "Toadflax potion (unf)", "Crushed nest"),
]:
    M(_m("Herblore", out, lvl, xp, [(unf, 1), (sec, 1)], [(out, 1)], members=True, rate=2500))

# ---------------- Prayer and Construction
METHODS += [
    _m("Prayer", "Bury big bones", 1, 15, [("Big bones", 1)], [], rate=2000),
    _m("Prayer", "Bury dragon bones", 1, 72, [("Dragon bones", 1)], [], rate=2000),
    _m("Prayer", "Dragon bones on gilded altar", 1, 252, [("Dragon bones", 1)], [], members=True, rate=1300),
    _m("Prayer", "Superior dragon bones on gilded altar", 70, 525, [("Superior dragon bones", 1)], [], members=True, rate=1300),
    _m("Construction", "Oak larder", 33, 480, [("Oak plank", 8)], [], members=True, rate=450),
    _m("Construction", "Mahogany table", 52, 840, [("Mahogany plank", 6)], [], members=True, rate=400),
    _m("Construction", "Oak dungeon door", 74, 600, [("Oak plank", 10)], [], members=True, rate=500),
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
    per_hour: float = 0.0       # actions per hour after buy-limit caps
    limited_by: str = ""        # the item whose GE buy limit caps the rate, if any

    @property
    def per_xp(self) -> float | None:
        return self.profit / self.xp if self.xp > 0 else None

    @property
    def profit_hour(self) -> float:
        return self.profit * self.per_hour

    @property
    def xp_hour(self) -> float:
        return self.xp * self.per_hour


def _find(by_name: dict, name: str):
    for alt in name.split("|"):
        iid = by_name.get(alt.lower())
        if iid is not None:
            return iid, alt
    return None, name.split("|")[0]


def eval_method(m: dict, by_name: dict, mapping: dict, latest: dict, h1: dict, d1: dict, instant: bool = False) -> MethodResult:
    success = m["success"] / 100
    cost = value = 0.0
    missing = []
    members = m["members"]
    rate = float(m.get("rate", 1200))
    limited_by = ""
    for name, qty in m["inputs"]:
        iid, label = _find(by_name, name)
        q = quote(iid, latest, h1, d1) if iid is not None else None
        if not q:
            missing.append(label)
            continue
        members = members or bool(mapping[iid].get("members"))
        cost += (q.inst_buy if instant else q.p_buy) * qty
        limit = mapping[iid].get("limit") or 0
        if limit and limit / LIMIT_HOURS / qty < rate:   # you can only buy this many per 4 hours
            rate, limited_by = limit / LIMIT_HOURS / qty, f"{label} buy limit"
        vol = sum(volumes(iid, h1, d1)) * SHARE / qty
        if vol and vol < rate:                           # not enough of it trades per hour
            rate, limited_by = vol, f"{label} trade volume"
    for name, qty in m["outputs"]:
        iid, label = _find(by_name, name)
        q = quote(iid, latest, h1, d1) if iid is not None else None
        if not q:
            missing.append(label)
            continue
        members = members or bool(mapping[iid].get("members"))
        value += net_price(iid, q.inst_sell if instant else q.p_sell) * qty * success
        vol = sum(volumes(iid, h1, d1)) * SHARE / (qty * success)
        if vol and vol < rate:                           # you can't sell more than the market takes
            rate, limited_by = vol, f"{label} trade volume"
    return MethodResult(m, m["xp"] * success, value - cost, cost, members, missing, rate, limited_by)
