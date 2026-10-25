"""把「人话」落到真实 SKU 上：解析、拆套餐、找备选、标未满足、算热量、核价。

原则：
- 不编造。名字对不上就是没对上，标成未满足并给人选，绝不拿相似商品顶包。
- 套餐一定拆开。卡片上写「三件套」等于什么都没说。
- 备选来自套餐真实可选项（含差价），不是我拍脑袋想的。
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field

import mcd_client
import menu as menu_mod
from roster import ME, Item, Roster

# 口语 -> 规范词。键是归一化后的用户说法，值是拿去和菜单名比对的词。
ALIASES = {
    "双吉": "双层吉士汉堡",
    "吉士": "吉士汉堡",
    "板烧": "板烧鸡腿堡",
    "麦辣鸡": "麦辣鸡腿堡",
    "麦辣鸡腿": "麦辣鸡腿堡",
    "麦香鱼": "麦香鱼",
    "巨无霸": "巨无霸",
    "bigmac": "巨无霸",
    "安格斯": "安格斯",
    "奥利奥麦旋风": "奥利奥麦旋风",
    "麦旋风": "麦旋风",
    "新地": "新地",
    "圣代": "新地",
    "中薯": "中薯条",
    "大薯": "大薯条",
    "小薯": "小薯条",
    "薯条": "薯条",
    "可乐": "可乐",
    "零度": "无糖可口可乐",
    "无糖可乐": "无糖可口可乐",
    "雪碧": "雪碧",
    "芬达": "芬达",
    "麦乐鸡": "麦乐鸡",
    "鸡块": "麦乐鸡",
    "鸡翅": "鸡翅",
    "咖啡": "麦咖啡",
    "拿铁": "拿铁",
    "美式": "美式",
    "橙汁": "橙汁",
    "随心配": "随心配",
    "1+1": "随心配",
    "儿童餐": "开心乐园",
    "开心乐园": "开心乐园",
    "派": "派",
    "玉米": "玉米杯",
    "沙拉": "沙拉",
    "汉堡": "汉堡",
}

SPICY = ("辣", "麻辣", "香辣", "藤椒", "椒香", "川")
BURGER = ("堡", "汉堡")
DRINK = ("可乐", "雪碧", "芬达", "咖啡", "拿铁", "美式", "茶", "汁", "奶", "水", "饮")
SIDE = ("薯", "鸡块", "麦乐鸡", "翅", "派", "玉米", "沙拉", "条")
SHARE = ("拼盘", "分享", "桶", "全家", "多人", "双人", "十块", "10块", "盒装")

# 非早餐时段不该出现的品项：菜单接口不按时段过滤干净，只能自己挡
BREAKFAST_ONLY = ("松饼", "油条", "粥", "麦满分", "猪柳蛋", "炒蛋", "豆浆", "薯饼", "早餐", "咖啡组合")

DEFAULT_CATEGORY = ("人气热卖", "精选单人餐")

THRESHOLD = 0.52


@dataclass
class Options:
    budget: float | None = None
    note: str = ""
    meal: str = ""
    per_person_limit: float | None = None
    top_k: int = 1
    swaps: dict = field(default_factory=dict)  # "人名|第几项" -> {round: choiceCode}
    no_party: bool = False  # 用户说「不要多人餐，各点各的」
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 名字归一化


def normalize(text: str) -> str:
    s = str(text or "").strip().lower()
    s = s.replace("（", "(").replace("）", ")").replace("，", "").replace(" ", "")
    s = re.sub(r"[x×*]\d+$", "", s)
    s = re.sub(r"[!！。.~～]+$", "", s)
    return s


def expand(query: str) -> str:
    q = normalize(query)
    if q in ALIASES:
        return ALIASES[q]
    for key in sorted(ALIASES, key=len, reverse=True):
        if key in q:
            return ALIASES[key]
    return q


def _hint_bonus(q: str, name: str) -> float:
    bonus = 0.0
    if any(k in q for k in BURGER) and any(k in name for k in BURGER):
        bonus += 0.03
    if any(k in q for k in DRINK) and any(k in name for k in DRINK):
        bonus += 0.03
    if any(k in q for k in SIDE) and any(k in name for k in SIDE):
        bonus += 0.03
    return bonus


BEEF_WORDS = ("牛", "吉士", "安格斯", "巨无霸")


def _wants_spicy(q: str) -> bool:
    return any(k in q for k in SPICY) or "辣" in q


def score(query: str, dish: menu_mod.Dish) -> float:
    """纯字符串相似度。口味/肉类的硬约束放到 rank() 里做，
    否则「辣的汉堡」会被 expand 成「汉堡」，辣味信号在中途丢掉。"""
    q = normalize(query)
    name = normalize(dish.name)
    if not q or not name:
        return 0.0
    if q == name:
        base = 1.0
    elif name.startswith(q):
        base = 0.92 - min(0.12, max(0, len(name) - len(q)) * 0.02)
    elif q in name:
        base = 0.86 - min(0.10, max(0, len(name) - len(q)) * 0.015)
    elif name in q:
        base = 0.74
    else:
        base = difflib.SequenceMatcher(None, q, name).ratio() * 0.82
    return round(min(1.0, base + _hint_bonus(q, name)), 4)


def rank(query: str, dishes: list[menu_mod.Dish]) -> list[tuple[float, menu_mod.Dish]]:
    q = normalize(query)
    qa = expand(query)
    want_spicy = _wants_spicy(q)
    want_beef = "牛" in q
    want_combo = any(k in q for k in ("套餐", "套", "组合", "三件套", "四件套"))
    want_burger = any(k in q for k in BURGER)
    # 一个人报「牛肉汉堡」不能配成双人餐——多花钱还吃不完。多人餐只走合并逻辑。
    want_party = any(k in q for k in PARTY_WORDS) or any(k in q for k in ("一起", "大家", "拼", "团"))

    rows = []
    for dish in dishes:
        n = normalize(dish.name)
        if not want_party and any(k in n for k in PARTY_WORDS):
            continue
        s = max(score(q, dish), score(qa, dish))
        spicy = any(k in n for k in SPICY)
        literal_beef = any(k in n for k in ("牛肉", "牛堡", "厚牛"))
        implied_beef = any(k in n for k in BEEF_WORDS)
        is_burger = any(k in n for k in BURGER)

        # 属性分层：说了辣就只推辣的，说了牛就先推名字里带牛的。
        # 实测踩过：王总要「辣的汉堡」被配成双层吉士，这错不能出第二次。
        tier = 0
        if want_beef:
            tier = 0 if literal_beef else (1 if implied_beef else 4)
        elif want_spicy:
            tier = 0 if spicy else 4
        if want_burger and not is_burger:
            tier += 1
        # 「XX单品精选」「XX随心选」是拿券换的组合商品（价是起价、给什么是店里定），
        # 不能当成一道确定的菜推荐。
        if any(k in n for k in ("单品精选", "随心选", "盲盒")) or (
            "精选" in n and not any(k in n for k in ("套餐", "三件套", "两件套", "单人餐"))
        ):
            tier += 1
        if tier == 0 and s < 0.62:
            s = 0.62  # 属性完全对上，即便名字不像也算对上，别掉到阈值下面
        if s <= 0.2:
            continue
        if want_combo and dish.is_combo_hint:
            s = min(1.0, s + 0.06)
        rows.append((tier, s, dish))

    if want_combo:
        combos = [r for r in rows if r[2].is_combo_hint and r[1] >= 0.7]
        if combos:
            rows = combos
    rows.sort(key=lambda r: (r[0], -r[1], len(r[2].name)))
    return [(s, d) for _t, s, d in rows]


# ------------------------------------------------------------------- 规划主流程


def build(roster: Roster, ctx: menu_mod.Context, menu: menu_mod.Menu, opts: Options) -> dict:
    flags: list[str] = []
    warnings: list[str] = list(roster.warnings)
    people_out: list[dict] = []
    shared_out: list[dict] = []
    lines: list[dict] = []
    dish_cache: dict[str, menu_mod.Combo] = {}
    food = menu.food_only()
    if opts.meal and opts.meal != "早餐":
        kept = [d for d in food if not any(k in d.name for k in BREAKFAST_ONLY)]
        if len(kept) >= 8:
            food = kept

    any_seq = 0
    for idx, person in enumerate(roster.people, start=1):
        entry = {
            "id": f"p{idx}",
            "name": person.name,
            "spoken": person.spoken,
            "avoid": person.avoid,
            "light": person.light,
            "items": [],
            "subtotal": 0.0,
            "kcal": 0.0,
            "kcal_complete": True,
        }
        if not person.items:
            person.items = [Item(name="随便", intent="any")]

        all_any = True  # 整份都是「随便」的人才允许被并进多人餐；有明确需求的不能被平均掉
        for item_idx, item in enumerate(person.items):
            forced = opts.swaps.get(f"{person.name}|{item_idx}") or {}
            resolved = _resolve_item(
                item, person, food, ctx, dish_cache, menu, forced,
                first=(item_idx == 0), any_offset=any_seq,
            )
            wants_any = getattr(item, "intent", None) == "any" or normalize(item.name) in ("随便", "都行")
            if wants_any:
                any_seq += 1
            else:
                all_any = False
            if resolved is None:
                continue
            entry["items"].append(resolved["entry"])
            if resolved["entry"].get("share"):
                shared_out.append({**resolved["entry"], "owner": person.name})
            if resolved["line"]:
                lines.append({**resolved["line"], "_who": person.name})

        if not entry["items"]:
            entry["items"].append(
                {
                    "name": "未匹配",
                    "qty": 1,
                    "unit": 0.0,
                    "subtotal": 0.0,
                    "kcal": None,
                    "parts": [],
                    "why": "没有可用餐品",
                    "alts": [],
                    "issues": ["菜单里没有对上任何商品"],
                }
            )
        entry["subtotal"] = round(sum(i["subtotal"] for i in entry["items"]), 2)
        entry["kcal"] = round(sum(i["kcal"] or 0 for i in entry["items"]), 1)
        entry["kcal_complete"] = all(i.get("kcal") is not None for i in entry["items"])
        entry["all_any"] = all_any
        people_out.append(entry)

    party = None
    if not opts.no_party:
        lines, party = _merge_party(people_out, food, lines, ctx, dish_cache)
    pricing = menu_mod.price(ctx, lines) if lines else _empty_price()
    _fill_units(people_out, pricing)
    _resplit_party(people_out, party)
    _attach_take_way(people_out, pricing, ctx)

    payable = pricing.get("payable") or 0.0
    count = len([p for p in people_out if p["items"]])
    avg = round(payable / count, 2) if count else 0.0

    budget = opts.budget if opts.budget is not None else roster.budget
    if budget and payable > budget + 1e-6:
        gap = round(payable - budget, 2)
        fix = _cheapest_fix(people_out, gap)
        flags.append(f"比预算多 ¥{gap:.2f}" + (f"；{fix}" if fix else "；没有更省的换法，只能砍品项或抬预算"))

    missing = [p["name"] for p in people_out if any(i["issues"] for i in p["items"])]
    if missing:
        shown = "、".join(_disp_all(missing[:3])) + ("等" if len(missing) > 3 else "")
        flags.append(f"{len(missing)} 人的项需要确认（{shown}）")

    plan = {
        "version": 2,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "context": {
            "store": ctx.store or {},
            "place": opts.extra.get("place", ""),
            "order_type": ctx.order_type,
            "be_type": ctx.be_type,
            "be_code": ctx.be_code,
            "address_id": ctx.address_id,
            "reservation": ctx.reservation,
            "take_way": ctx.take_way_code,
            "take_way_title": ctx.take_way_title,
            "meal": opts.meal,
            "budget": budget,
            "note": opts.note,
        },
        "summary": {
            "people": count,
            "item_count": sum(len(p["items"]) for p in people_out),
            "payable": payable,
            "product_price": pricing.get("product_price", 0.0),
            "discount": pricing.get("discount", 0.0),
            "fees": pricing.get("fees", 0.0),
            "avg": avg,
            "kcal_total": round(sum(p["kcal"] or 0 for p in people_out), 1),
        },
        "people": people_out,
        "shared": shared_out,
        "party": party,
        "pricing": pricing,
        "flags": flags,
        "warnings": warnings,
    }
    return plan


# ------------------------------------------------------------------- 单项解析


def _resolve_item(item, person, food, ctx, dish_cache, menu, forced: dict | None = None, first: bool = True,
                  any_offset: int = 0) -> dict | None:
    issues: list[str] = []
    adjust: list[str] = []

    packed = None
    whole_alts: list[dict] = []
    rows: list = []
    if item.intent == "any" or normalize(item.name) in ("随便", "都行"):
        dish, whole_alts = _default_dish(person, food, offset=any_offset)
    else:
        # 「麦乐鸡10块」这种把份数写在名字里的，别再乘一遍数量
        packed = _pack_item(item, food)
        if packed:
            dish, item = packed[0], packed[1]
            rows = [(1.0, dish)]
        else:
            rows = rank(item.name, food)
        rows = [(s, d) for s, d in rows if not _conflicts(d.name, person)]
        if not rows:
            rows_any = rank(item.name, food)
            near = [d.name for _s, d in rows_any[:3]]
            entry = {
                "name": item.name,
                "qty": item.qty,
                "unit": 0.0,
                "subtotal": 0.0,
                "kcal": None,
                "parts": [],
                "why": "菜单里没有这个",
                "alts": [{"label": n, "diff": None, "round": None, "code": None, "kcal": None} for n in near],
                "issues": [f"「{item.name}」这家店当前菜单里没有" + (f"，最近的替代：{'、'.join(near[:2])}" if near else "")],
            }
            return {"entry": entry, "line": None}
        best_score, dish = rows[0]
        if best_score < THRESHOLD:
            near = [d.name for _s, d in rows[:3]]
            entry = {
                "name": item.name,
                "qty": item.qty,
                "unit": 0.0,
                "subtotal": 0.0,
                "kcal": None,
                "parts": [],
                "why": "没对上菜名",
                "alts": [{"label": n, "diff": None, "round": None, "code": None, "kcal": None} for n in near],
                "issues": [f"「{item.name}」没对上具体商品，需要本人确认"],
            }
            return {"entry": entry, "line": None}

    if item.qty >= 3 and not packed:
        issues.append(f"「{item.qty}」按份数算的，如果指的是盒装规格（比如 10 块装）请改一下")

    combo = _combo_of(dish, ctx, dish_cache)
    parts = combo.default_parts() if combo and combo.is_combo else []
    overrides, swap_notes = _apply_requests(combo, person, item)
    if forced and combo:
        for rnd, code in forced.items():
            overrides[str(rnd)] = code
            hit = next((c for r in combo.rounds for c in r.choices if c.code == code), None)
            if hit:
                swap_notes.append(f"换成{hit.name}")
    if overrides and combo:
        parts = []
        for r in combo.rounds:
            choice = next((c for c in r.choices if c.code == overrides.get(r.round)), None) or r.default_choice
            if choice:
                parts.append({"round": r.round, "title": r.title, "name": choice.name, "qty": choice.quantity})
        adjust.extend(swap_notes)

    kcal, kcal_ok = _kcal_of(combo, parts, dish)
    alts = _alts_of(combo, person, item, overrides)
    if whole_alts:
        # 「随便」：整份候选放前面（最多 2 个），套餐内换口味只留 1 个，别铺满
        alts = whole_alts[:2] + alts[:1]
    if not alts and rows:
        alts = _peer_alts(rows, dish, person)
    if kcal is None:
        kcal_ok = False  # 卡片会写「热量未收录」，但这不是要人拍板的事，不进 issues

    if _conflicts(dish.name, person):
        issues.append(f"这个含{_conflict_word(dish.name, person)}，跟忌口冲突，需要本人确认")

    why = _why(person, dish, combo, parts, item, first)
    line = {
        "productCode": dish.code,
        "quantity": item.qty,
        "roundList": combo.round_list(overrides) if combo and combo.is_combo else None,
    }
    entry = {
        "name": dish.name,
        "code": dish.code,
        "roundList": line["roundList"],  # 下单必须带，否则门店拿不到套餐内选了什么
        "qty": item.qty,
        "unit": dish.price or 0.0,
        "subtotal": round((dish.price or 0.0) * item.qty, 2),
        "kcal": kcal,
        "kcal_partial": not kcal_ok,
        "parts": parts,
        "why": why,
        "alts": alts,
        "issues": issues,
        "adjust": adjust,
        "note": item.note,
        "share": _is_share(dish, item),
    }
    return {"entry": entry, "line": line}


def _pack_item(item, food):
    """「麦乐鸡10块」里那句 10 说的是盒装规格，不是要 10 盒。
    找到名字里带同样数字的规格款，就把数量收回 1。"""
    if item.qty < 3:
        return None
    q = normalize(item.name)
    num = str(item.qty)
    for dish in food:
        n = normalize(dish.name)
        if num not in n or not any(u in n for u in ("块", "个", "只", "支", "片")):
            continue
        base = re.sub(r"\d+[块个只支片]", "", n)
        if q and (q in n or n.startswith(q) or score(q, dish) >= 0.55) and base:
            return dish, Item(name=item.name, qty=1, note=item.note, intent=item.intent)
    return None


def _combo_of(dish, ctx, cache) -> menu_mod.Combo | None:
    if dish.code in cache:
        return cache[dish.code]
    try:
        combo = menu_mod.load_combo(ctx, dish.code)
    except menu_mod.mcd_client.McdError:
        combo = None
    cache[dish.code] = combo
    return combo


def _apply_requests(combo, person, item) -> tuple[dict, list[str]]:
    """只在本人明确提出时才动套餐内的选择，否则保持默认。"""
    overrides: dict[str, str] = {}
    notes: list[str] = []
    if not combo or not combo.is_combo:
        return overrides, notes

    want_zero_sugar = any(k in (item.note or "") for k in ("无糖", "不要糖", "去糖", "零度"))
    want_large = "大" in (item.note or "")
    want_small = "小" in (item.note or "")

    for r in combo.rounds:
        others = [c for c in r.choices if not c.default]
        if not others:
            continue
        pick = None
        if want_zero_sugar and any(k in r.title for k in ("饮", "饮料")):
            pick = next((c for c in others if "无糖" in c.name or "零度" in c.name), None)
            if pick:
                notes.append(f"{_round_name(r.title)}换成{pick.name}（你要无糖）")
        if pick is None and want_large:
            pick = next((c for c in others if c.name.startswith("大")), None)
            if pick:
                notes.append(f"{_round_name(r.title)}换成{pick.name}（你要大份）")
        if pick is None and want_small:
            pick = next((c for c in others if c.name.startswith("小")), None)
            if pick:
                notes.append(f"{_round_name(r.title)}换成{pick.name}（你要小份）")
        if pick is None and person.avoid:
            default = r.default_choice
            if default and _conflicts(default.name, person):
                pick = next((c for c in others if not _conflicts(c.name, person)), None)
                if pick:
                    notes.append(f"{_round_name(r.title)}从{default.name}换成{pick.name}（避开{_conflict_word(default.name, person)}）")
        if pick is not None:
            overrides[r.round] = pick.code
    return overrides, notes


def _round_name(title: str) -> str:
    """「选择套餐内饮料」-> 「饮料」，卡片上读着像人话。"""
    t = re.sub(r"^(请)?选择?(套餐内|套餐中|套餐)?", "", title or "").strip()
    return t or (title or "子项")


def _alts_of(combo, person, item, overrides) -> list[dict]:
    """备选只从「套餐里真能换的东西」和「同类的另一款」里出，最多两条。"""
    alts: list[dict] = []
    if combo and combo.is_combo:
        for r in combo.rounds:
            chosen = overrides.get(r.round)
            default = r.default_choice
            current = next((c for c in r.choices if c.code == chosen), None) or default
            others = [c for c in r.choices if c is not current]
            if person.avoid:
                others = [c for c in others if not _conflicts(c.name, person)]
            # 优先无糖 / 差价小 / 大份
            others.sort(key=lambda c: (0 if ("无糖" in c.name and item.note and "糖" in item.note) else 1, abs(c.diff), -c.diff))
            for c in others[:2]:
                kcal = menu_mod.lookup_nutrition(c.name)
                alts.append(
                    {
                        "label": c.name,
                        "diff": round(c.diff, 2),
                        "round": r.round,
                        "code": c.code,
                        "title": r.title,
                        "kcal": (kcal or {}).get("kcal"),
                    }
                )
            if len(alts) >= 2:
                break
    return alts[:2]


def _peer_alts(rows, dish, person) -> list[dict]:
    """单品没有套餐内可换项时，给同类的另一款在售商品（真实价格、标差价），
    这样「这个不行」的时候也有东西可换，不用重新收单。"""
    alts: list[dict] = []
    for _s, other in rows:
        if other.name == dish.name or len(alts) >= 2:
            continue
        if _conflicts(other.name, person):
            continue
        diff = round((other.price or 0) - (dish.price or 0), 2)
        alts.append({"label": other.name, "diff": diff, "round": None, "code": other.code, "whole": True, "kcal": None})
    return alts


def _kcal_of(combo, parts, dish) -> tuple[float | None, bool]:
    """返回（热量, 是否逐项精确命中）。对不上就返回下限值并标不精确，绝不拿相似品凑数当准确值。"""
    if combo and combo.is_combo and parts:
        total, exact, found = 0.0, True, 0
        for part in parts:
            row = menu_mod.lookup_nutrition(part["name"])
            if row and row.get("kcal") is not None:
                total += row["kcal"] * (part.get("qty") or 1)
                found += 1
                if row.get("match") != "exact":
                    exact = False
            else:
                exact = False
        # 0 kcal 是真实值（无糖可乐），不能当成「没查到」
        return (round(total, 1) if found else None), exact
    row = menu_mod.lookup_nutrition(dish.name)
    if row and row.get("kcal") is not None:
        return float(row["kcal"]), row.get("match") == "exact"
    return None, False


def _why(person, dish, combo, parts, item, first: bool = True) -> str:
    bits = []
    spoken = (person.spoken or "").strip()
    if spoken and first:
        bits.append(f"你的原话是「{_clip(spoken, 16)}」")
    if getattr(item, "intent", None) == "any":
        bits.append("没说吃什么，按人气套餐配的，可以整份换")
    if person.avoid:
        bits.append(f"避开{'、'.join(person.avoid)}")
    if combo and combo.is_combo and parts:
        kinds = []
        if any(any(k in p["name"] for k in BURGER) for p in parts):
            kinds.append("主食")
        if any(any(k in p["name"] for k in SIDE) for p in parts):
            kinds.append("小食")
        if any(any(k in p["name"] for k in DRINK) for p in parts):
            kinds.append("饮料")
        if kinds:
            bits.append("含有" + "+".join(kinds))
        else:
            bits.append(f"含 {len(parts)} 个子项")
    elif dish.is_combo_hint:
        bits.append("套餐类，组成见上方明细")
    else:
        bits.append("单品，没配饮料和小食")
    if person.light and not (combo and len(parts) >= 3):
        bits.append("按你说的少一点配")
    if not bits:
        bits.append("常规份量")
    return "，".join(bits) + "。"


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _conflicts(name: str, person) -> bool:
    if not person.avoid:
        return False
    return _conflict_word(name, person) is not None


def _conflict_word(name: str, person) -> str | None:
    for key in person.avoid:
        if key == "辣":
            if any(w in name for w in SPICY):
                return key
            continue
        if key in name:
            return key
    return None


# ------------------------------------------------------------------- 多人餐


PARTY_WORDS = ("双人", "三人", "四人", "五人", "多人", "分享", "派对", "全家", "聚餐")


def _party_size(dish) -> int:
    """这套够几个人吃。「双人餐」是 2，「合家欢聚鸡牛三人餐」是 3。"""
    n = dish.name
    m = re.search(r"([2-9])\s*人", n)
    if m:
        return int(m.group(1))
    for word, size in (("双人", 2), ("三人", 3), ("四人", 4), ("五人", 5), ("全家", 3)):
        if word in n:
            return size
    return 2


def _party_options(food) -> list[tuple]:
    """能顶一顿饭的多人餐。纯鸡翅桶（超级翅桶、十翅桶）不算——没有主食。"""
    out = []
    for dish in food:
        if not dish.price or dish.price <= 0:
            continue
        n = dish.name
        if not any(k in n for k in PARTY_WORDS):
            continue
        if "桶" in n and not any(k in n for k in ("餐", "堡", "饭")):
            continue
        if not any(k in n for k in ("餐", "套", "桶", "拼盘")):
            continue
        out.append((dish, _party_size(dish)))
    out.sort(key=lambda x: (x[0].price or 0) / max(1, x[1]))
    return out


def _merge_party(people, food, lines, ctx, dish_cache) -> tuple[list[dict], dict | None]:
    """几个「随便」的人合成一份多人餐，前提是真的更省。

    有明确需求的人不合并——省下的钱不值得让想吃牛肉堡的人跟着吃鸡。
    省不到 8 块或 8% 就不动，否则卡片上会为几毛钱改菜单，反而让人多看一眼。
    """
    pool = [p for p in people if p.get("all_any") and p["items"] and p["items"][0].get("code")]
    if len(pool) < 2:
        return lines, None
    current = round(sum(p["subtotal"] for p in pool), 2)
    best = None
    for dish, size in _party_options(food):
        need = -(-len(pool) // size)
        cost = round((dish.price or 0) * need, 2)
        if cost >= current - max(1.0, current * 0.01):
            continue
        if best is None or cost < best["cost"]:
            best = {"dish": dish, "need": need, "cost": cost, "size": size}
    if not best:
        return lines, None

    dish = best["dish"]
    names = [p["name"] for p in pool]
    # 合并会把各人的单项抹掉，先记住「各点各的」原方案，
    # 作为每个人卡片上的一条备选——否则说了「随便」又不想合买的人无路可退。
    before = {
        p["name"]: {
            "name": (p["items"][0].get("name") or ""),
            "code": p["items"][0].get("code"),
            "subtotal": p.get("subtotal") or 0.0,
        }
        for p in pool
    }
    combo = _combo_of(dish, ctx, dish_cache)
    line = {
        "productCode": dish.code,
        "quantity": best["need"],
        "roundList": combo.round_list() if combo and combo.is_combo else None,
        "_who": names[0],
    }
    # 合并前先真核一次价。菜单价和实际核价会差（套餐轮次要选份数、某些口味本时段
    # 不售），照菜单价宣布「省 3 块」、下单却报 60006，比不合并更难收场。
    # 核不出来或核完不便宜，就当没这回事，各点各的。
    if ctx is not None:
        try:
            real = float(menu_mod.price(ctx, [line]).get("product_price") or 0.0)
        except mcd_client.McdError:
            return lines, None
        if real <= 0 or real >= current - max(1.0, current * 0.01):
            return lines, None
        best["cost"] = round(real, 2)

    lines = [l for l in lines if l.get("_who") not in names] + [line]

    share = round(best["cost"] / len(pool), 2)
    parts = combo.default_parts() if combo and combo.is_combo else []

    def split_alt(name: str, own: float) -> list[dict]:
        """「拆开各点」：合买的人唯一的退路，差价按本人原方案算。"""
        orig = before.get(name) or {}
        if not orig.get("name"):
            return []
        return [
            {
                "label": orig["name"],
                "diff": round(orig["subtotal"] - own, 2),
                "round": None,
                "code": orig.get("code"),
                "whole": True,
                "pre": "各点",
            }
        ]

    for idx, person in enumerate(pool):
        is_last = idx == len(pool) - 1
        own = round(best["cost"] - share * (len(pool) - 1), 2) if is_last else share
        person["subtotal"] = own
        person["kcal"] = None
        person["kcal_complete"] = False
        if idx == 0:
            person["items"] = [
                {
                    "name": dish.name,
                    "code": dish.code,
                    "roundList": line["roundList"],
                    "qty": best["need"],
                    "unit": dish.price or 0.0,
                    "subtotal": own,
                    "kcal": None,
                    "parts": parts,
                    "why": f"{len(pool)} 人合买 {best['need']} 份，比各点各的省 ¥{current - best['cost']:.2f}",
                    "alts": split_alt(person["name"], own),
                    "issues": [],
                    "adjust": [],
                    "note": "",
                    "share": True,
                    "party_for": names,
                }
            ]
        else:
            person["items"] = [
                {
                    "name": f"与{names[0]}共用「{dish.name}」",
                    "code": None,
                    "roundList": None,
                    "qty": 0,
                    "unit": own,
                    "subtotal": own,
                    "kcal": None,
                    "parts": [],
                    "why": f"并在 {len(pool)} 人多人餐里，分摊 ¥{own:.2f}",
                    "alts": split_alt(person["name"], own),
                    "issues": [],
                    "adjust": [],
                    "note": "",
                    "share": True,
                    "split_of": names[0],
                    "party_for": names,
                }
            ]
    return lines, {
        "name": dish.name,
        "code": dish.code,
        "need": best["need"],
        "cost": best["cost"],
        "saved": round(current - best["cost"], 2),
        "for": names,
    }


def _is_share(dish, item) -> bool:
    if any(k in dish.name for k in SHARE):
        return True
    return any(k in (item.note or "") for k in ("大家", "分着", "共享", "拼"))


# ---------------------------------------------------------------- 默认与人气


def _default_dish(person, food, offset: int = 0) -> tuple:
    """「随便」按大众口味配：人气分类里的经典单人套餐。
    返回（主选, 整份候选列表）。候选是真实在售的其他人气套餐，标清差价，
    卡片上直接写「整份换成 XX（+¥3）」，用户说「换第 2 个」就能换。
    offset 让同一单里多个「随便」的人拿到不同套餐——三个人吃一样，等于没照顾人。"""
    pool = [d for d in food if not _conflicts(d.name, person)] or food
    combos = [d for d in pool if d.is_combo_hint]
    # 单人份优先：双人餐/全家桶留给 _merge_party 去合并，别塞给一个人
    solo = [d for d in combos if not any(k in d.name for k in PARTY_WORDS)]
    if solo:
        combos = solo

    def rank_key(d):
        # 秩序：正经单人套餐 > 其他 > 随心配（1+1，分量不够一顿）> 四件套/双人份
        # 四个人吃一样等于没照顾人，但轮换也不能把人均从 20 抬到 58。
        n = d.name
        if any(k in n for k in ("四件套", "双人", "多人", "分享", "全家", "桶")):
            form = 3
        elif "随心配" in n:
            form = 2
        elif any(k in n for k in ("三件套", "两件套", "单人餐", "套餐")):
            form = 0
        else:
            form = 1
        for i, cat in enumerate(DEFAULT_CATEGORY):
            if d.category.startswith(cat):
                return (form, i, d.price or 0)
        return (form, len(DEFAULT_CATEGORY), d.price or 0)

    combos.sort(key=rank_key)
    # 同名不同 code 的只留一个
    seen: set[str] = set()
    ranked = []
    for d in combos:
        if d.name in seen:
            continue
        seen.add(d.name)
        ranked.append(d)
    if not ranked:
        ranked = sorted((d for d in pool if any(k in d.name for k in BURGER)), key=lambda d: d.price or 0) or pool or food

    head = ranked[:3]
    chosen = head[offset % len(head)] if head else ranked[0]
    alts = []
    for d in ranked:
        if d.name == chosen.name or len(alts) >= 2:
            continue
        diff = round((d.price or 0) - (chosen.price or 0), 2)
        alts.append({"label": d.name, "diff": diff, "round": None, "code": d.code, "whole": True, "kcal": None})
    return chosen, alts


# -------------------------------------------------------------------- 价格补齐


def _empty_price() -> dict:
    return {
        "product_price": 0.0,
        "original_price": 0.0,
        "discount": 0.0,
        "fees": 0.0,
        "payable": 0.0,
        "unit": {},
        "take_way": [],
        "items": [],
    }


def _fill_units(people_out, pricing) -> None:
    """每个商品行的实付单价取核价结果，不用菜单标价（组合类标价是起价）。"""
    unit_map = pricing.get("unit") or {}
    for person in people_out:
        for item in person["items"]:
            code = item.get("code")
            if not code or code not in unit_map:
                continue
            unit = unit_map[code]["unit"]
            item["unit"] = unit
            item["subtotal"] = round(unit * item["qty"], 2)
        person["subtotal"] = round(sum(i["subtotal"] for i in person["items"]), 2)


def _resplit_party(people_out, party) -> None:
    """核价后按人头分摊。owner 那行照旧显示整份价，个人小计只承担自己那份——
    否则几个人加起来会比应付多出一整份。"""
    if not party:
        return
    members = [p for p in people_out if p["name"] in party["for"]]
    if not members:
        return
    total = party["cost"]
    share = round(total / len(members), 2)
    for idx, person in enumerate(members):
        last = idx == len(members) - 1
        person["subtotal"] = round(total - share * (len(members) - 1), 2) if last else share


def _attach_take_way(people_out, pricing, ctx) -> None:
    if ctx.order_type == 1 and not getattr(ctx, "take_way_code", None):
        ways = pricing.get("take_way") or []
        prefer = next((w for w in ways if w["code"] in ("eat-in", "take-out")), None) or (ways[0] if ways else None)
        if prefer:
            ctx.take_way_code = prefer["code"]
            ctx.take_way_title = prefer["title"]
    elif ctx.order_type == 2 and not getattr(ctx, "take_way_title", None):
        # 外送没有 takeWayCode，卡片上不填就会写成「到店自取」——送餐单写成自取，是硬错。
        ctx.take_way_title = "麦乐送"


def _cheapest_fix(people_out, gap: float) -> str:
    """给一条「怎么降到预算内」的具体建议：在一步或两步之内找最省的那组动作。"""
    moves: list[tuple[float, str]] = []
    for person in people_out:
        for item in person["items"]:
            for alt in item.get("alts") or []:
                diff = alt.get("diff")
                if diff is None:
                    continue
                if diff < 0:
                    moves.append((round(-diff * item["qty"], 2), f"{_name(person['name'])}的{item['name']}换成{alt.get('label', '')}"))
            qty = item.get("qty", 1)
            spec_doubt = "规格" in " ".join(item.get("issues") or [])
            if qty >= 3 and spec_doubt:
                # 数量本身可疑时，按「其实是规格」算最省钱的那一档
                moves.append((round(item["unit"] * (qty - 1), 2), f"{_name(person['name'])}的{item['name']}按 1 份算"))
            elif qty > 1 and not spec_doubt:
                moves.append((round(item["unit"], 2), f"{_name(person['name'])}的{item['name']}减一份"))

    if not moves:
        return ""
    moves.sort(key=lambda m: -m[0])

    # 一步能补上就只动一步；否则两步里找刚刚够、且动得最少的那组
    best: tuple[float, list[str]] | None = None
    for i, (gain, text) in enumerate(moves[:6]):
        if gain >= gap and (best is None or gain < best[0]):
            best = (gain, [text])
        for gain2, text2 in moves[i + 1: i + 6]:
            if gain + gain2 >= gap and (best is None or gain + gain2 < best[0]):
                best = (gain + gain2, [text, text2])

    if best is None:
        top = moves[:2]
        total = round(sum(g for g, _ in top), 2)
        return "、".join(f"{t}（-¥{g:.2f}）" for g, t in top) + f"，合计 -¥{total:.2f}，仍差 ¥{gap - total:.2f}"

    total, picked = best
    return "、".join(f"{t}（-¥{g:.2f}）" for g, t in zip(_gains(picked, moves), picked)) + f"，合计 -¥{total:.2f}"


def _gains(picked: list[str], moves: list[tuple[float, str]]) -> list[float]:
    pool = {t: g for g, t in moves}
    return [pool.get(t, 0.0) for t in picked]


def alert_label(alt: dict) -> str:
    diff = alt.get("diff")
    if diff is None:
        return alt.get("label", "")
    sign = "+" if diff > 0 else ("-" if diff < 0 else "±")
    return f"{alt['label']}（{sign}¥{abs(diff):.2f}）"


def _name(name: str) -> str:
    return "你" if name == ME else name


def _disp_all(names: list[str]) -> list[str]:
    return [_name(n) for n in names]
