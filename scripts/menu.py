"""菜单域：门店、在售菜单、套餐组成、核价、营养。

这一层只跟「数据」打交道，不认识「人」也不认识「卡片」。
所有金额在这里统一换算成「元」（接口核价返回的是分）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import mcd_client
from toon import parse_toon_table

CENTS = 100.0

# 会员卡 / 权益卡不是吃的，出现在菜单里但绝不该被当成一道菜
NOT_FOOD = ("麦金卡", "早餐卡", "会员卡", "权益卡", "优惠券包")


@dataclass
class Context:
    """一次点餐的固定上下文。四个接口必须用同一份。"""

    store_code: str
    order_type: int = 1  # 1 到店 / 2 外送
    be_type: int = 1  # 1 到店取 / 2 麦乐送 / 5 得来速 / 6 团餐
    be_code: str | None = None
    address_id: str | None = None
    reservation: str | None = None  # "yyyy-MM-dd HH:mm"
    store: dict = field(default_factory=dict)  # 门店原始信息，卡片抬头用
    take_way_code: str | None = None
    take_way_title: str | None = None

    def args(self) -> dict:
        a = {"storeCode": self.store_code, "orderType": self.order_type, "beType": self.be_type}
        if self.order_type == 2 and self.be_code:
            a["beCode"] = self.be_code
        if self.be_type in (2, 5, 6) and self.be_code:
            a["beCode"] = self.be_code
        if self.reservation:
            a["reservationDate"] = self.reservation
        return a


@dataclass
class Dish:
    code: str
    name: str
    price: float | None
    original: float | None
    category: str
    tags: list[str] = field(default_factory=list)

    @property
    def is_combo_hint(self) -> bool:
        return any(k in self.name for k in ("套餐", "三件套", "四件套", "两件套", "随心配", "组合", "分享"))


@dataclass
class Choice:
    code: str
    name: str
    default: bool
    diff: float
    quantity: int


@dataclass
class Round:
    round: str
    title: str
    min_qty: int
    max_qty: int
    choices: list[Choice]

    @property
    def default_choice(self) -> Choice | None:
        for c in self.choices:
            if c.default:
                return c
        return self.choices[0] if self.choices else None

    def qty_for(self, choice: Choice) -> int:
        """这一轮该给几份。

        接口返回的 choice.quantity 靠不住：双人餐的饮料轮 minQuantity=2，但默认
        选项上写 quantity=1 —— 照抄会在核价时报 60006「商品不可售」，看着像门店
        缺货，其实是份数不够。份数以下限 minQuantity 为准，选项自带的更大就用它。
        """
        return max(int(self.min_qty or 1), int(choice.quantity or 1))


@dataclass
class Combo:
    code: str
    name: str
    rounds: list[Round]

    @property
    def is_combo(self) -> bool:
        return len(self.rounds) > 0

    def default_parts(self) -> list[dict]:
        parts = []
        for r in self.rounds:
            c = r.default_choice
            if c:
                parts.append({"round": r.round, "title": r.title, "name": c.name, "qty": r.qty_for(c)})
        return parts

    def round_list(self, overrides: dict | None = None) -> list[dict]:
        """拼 create-order / calculate-price 需要的 roundList。"""
        overrides = overrides or {}
        out = []
        for r in self.rounds:
            pick = overrides.get(r.round)
            choice = None
            if pick:
                choice = next((c for c in r.choices if c.code == pick or c.name == pick), None)
            if choice is None:
                choice = r.default_choice
            if choice is None:
                continue
            out.append({"round": r.round, "comboItemList": [{"code": choice.code, "quantity": r.qty_for(choice)}]})
        return out


@dataclass
class Menu:
    ctx: Context
    dishes: list[Dish]
    categories: list[str]

    def by_code(self, code: str) -> Dish | None:
        return next((d for d in self.dishes if d.code == code), None)

    def food_only(self) -> list[Dish]:
        return [d for d in self.dishes if not _is_not_food(d)]


def _is_not_food(dish: Dish) -> bool:
    return any(k in dish.name for k in NOT_FOOD) or any(t in NOT_FOOD for t in dish.tags)


# ------------------------------------------------------------------ 门店 / 地址


def find_stores(city: str, keyword: str, *, be_type: int = 1, search_type: int = 2) -> list[dict]:
    """找店。city 与 keyword 必须同时给（缺一个直接 600058）。"""
    if not city or not keyword:
        raise mcd_client.McdError("BAD_ARGS", "查店必须同时给 city 和 keyword", tool="query-nearby-stores")
    data = mcd_client.call(
        "query-nearby-stores", {"beType": be_type, "searchType": search_type, "city": city, "keyword": keyword}, ttl=600
    )
    rows = data if isinstance(data, list) else data.get("data") or data.get("stores") or []
    return [r for r in rows if isinstance(r, dict)]


def open_stores(stores: list[dict]) -> list[dict]:
    return [s for s in stores if s.get("businessStatus") is not False]


def address_text(row: dict) -> str:
    """账号地址拼成一行人话。实测接口只给 fullAddress，老的 city/address 字段是空的，
    按字段顺序拼会把 None 拼进卡片里。"""
    full = (row.get("fullAddress") or "").strip()
    if full:
        return full
    bits = [row.get(k) for k in ("province", "city", "district", "address", "addressDetail")]
    return "".join(str(b).strip() for b in bits if b and str(b).strip())


def addresses() -> list[dict]:
    """账号里已存的配送地址。返回原字段 + 归一化的 text/contact，调用方不用再猜结构。"""
    data = mcd_client.call("delivery-query-addresses", {}, ttl=60)
    rows = data if isinstance(data, list) else data.get("data") or data.get("addresses") or []
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("addressId"):
            continue
        row = dict(r)
        row["text"] = address_text(row)
        row["contact"] = row.get("contactName") or ""
        row["phone_masked"] = row.get("phone") or ""
        out.append(row)
    return out


def delivery_stores(address_id: str, be_type: int = 2) -> list[dict]:
    data = mcd_client.call("delivery-query-stores", {"beType": be_type, "addressId": address_id}, ttl=300)
    rows = data if isinstance(data, list) else data.get("data") or data.get("stores") or []
    return [r for r in rows if isinstance(r, dict)]


# ------------------------------------------------------------------------ 菜单


def load_menu(ctx: Context) -> Menu:
    data = mcd_client.call("query-meals", ctx.args(), ttl=mcd_client.DEFAULT_TTL)
    diet = (data or {}).get("meals") or {}
    categories: list[str] = []
    owner: dict[str, str] = {}
    for cat in (data or {}).get("categories") or []:
        title = re.sub(r"\s+", "", str(cat.get("name") or ""))
        if title:
            categories.append(title.replace("//", "/"))
        for m in cat.get("meals") or []:
            code = str(m.get("code") or "")
            if code and code not in owner:
                owner[code] = title.replace("//", "/")

    dishes = []
    for code, item in diet.items():
        if not isinstance(item, dict) or not item.get("name"):
            continue
        dishes.append(
            Dish(
                code=str(code),
                name=str(item["name"]).strip(),
                price=_menu_yuan(item.get("currentPrice")),
                original=_menu_yuan(item.get("originalPrice")),
                category=owner.get(str(code), "其他"),
                tags=[str(t) for t in (item.get("tags") or [])],
            )
        )
    return Menu(ctx=ctx, dishes=dishes, categories=categories)


def load_combo(ctx: Context, code: str) -> Combo:
    data = mcd_client.call("query-meal-detail", {**ctx.args(), "code": code}, ttl=mcd_client.DEFAULT_TTL)
    rounds: list[Round] = []
    for r in (data or {}).get("rounds") or []:
        choices = [
            Choice(
                code=str(c.get("code") or ""),
                name=str(c.get("name") or "").strip(),
                default=bool(c.get("isDefault")),
                diff=_yuan(c.get("diffPrice")) or 0.0,
                quantity=int(c.get("quantity") or 1),
            )
            for c in (r.get("choices") or [])
            if c.get("name")
        ]
        rounds.append(
            Round(
                round=str(r.get("id") or len(rounds) + 1),
                title=re.sub(r"\s+", "", str(r.get("name") or "")),
                min_qty=int(r.get("minQuantity") or 0),
                max_qty=int(r.get("maxQuantity") or 1),
                choices=choices,
            )
        )
    name = str((data or {}).get("name") or code)
    return Combo(code=str(code), name=name, rounds=rounds)


# ------------------------------------------------------------------------ 核价


def price(ctx: Context, lines: list[dict]) -> dict:
    """lines: [{'productCode','quantity','roundList'?,'couponId'?}]"""
    items = []
    for line in lines:
        item = {"productCode": str(line["productCode"]), "quantity": int(line.get("quantity") or 1)}
        if line.get("roundList"):
            item["roundList"] = line["roundList"]
        if line.get("couponId"):
            item["couponId"] = line["couponId"]
        items.append(item)

    data = mcd_client.call("calculate-price", {**ctx.args(), "items": items}, ttl=0) or {}
    product = _yuan(data.get("productPrice")) or 0.0
    original = _yuan(data.get("originalPrice")) or product
    discount = _yuan(data.get("discount")) or 0.0
    payable = _yuan(data.get("price")) or 0.0
    fees = round(payable - product + discount, 2)

    unit = {}
    for row in data.get("productList") or []:
        code = str(row.get("productCode") or "")
        qty = int(row.get("quantity") or 1) or 1
        subtotal = _yuan(row.get("subtotal")) or 0.0
        unit.setdefault(code, {"subtotal": 0.0, "quantity": 0, "unit": 0.0})
        unit[code]["subtotal"] = round(unit[code]["subtotal"] + subtotal, 2)
        unit[code]["quantity"] += qty
        unit[code]["unit"] = round(unit[code]["subtotal"] / unit[code]["quantity"], 2)

    take_way = [
        {"code": str(t.get("code")), "title": str(t.get("title") or "")}
        for t in (data.get("takeWayList") or [])
        if t.get("code")
    ]
    return {
        "items": items,
        "product_price": product,
        "original_price": original,
        "discount": discount,
        "fees": fees,
        "payable": payable,
        "unit": unit,
        "take_way": take_way,
        "raw_keys": sorted(data.keys()),
    }


# ------------------------------------------------------------------------ 营养


_NUTRI_CACHE: dict[str, dict] = {}


def nutrition() -> dict[str, dict]:
    """餐品营养表。返回 {name: {kcal, protein, fat, carb, sodium, calcium}}。"""
    global _NUTRI_CACHE
    if _NUTRI_CACHE:
        return _NUTRI_CACHE
    data = mcd_client.call("list-nutrition-foods", {}, ttl=3600)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    rows = parse_toon_table(text)
    table = {}
    for row in rows:
        name = (row.get("productName") or "").strip()
        if not name:
            continue
        table[name] = {
            "kcal": _num(row.get("energyKcal")),
            "protein": _num(row.get("protein")),
            "fat": _num(row.get("fat")),
            "carb": _num(row.get("carbohydrate")),
            "sodium": _num(row.get("sodium")),
            "calcium": _num(row.get("calcium")),
        }
    _NUTRI_CACHE = table
    return table


def lookup_nutrition(name: str) -> dict | None:
    """营养表按商品名对，但菜单名和营养表名常常差一个杯型/规格，
    所以依次试：原名、加中/大/小前缀、去括号、互相包含。"""
    table = nutrition()
    if name in table:
        return {**table[name], "match": "exact"}
    variants = [name]
    for pre in ("中", "大", "小"):
        variants.append(pre + name)
    for pre in ("中杯", "大杯", "小杯", "中", "大", "小"):
        variants.append(pre + name)
    for v in variants:
        if v in table:
            return {**table[v], "match": "variant"}
    # 「无糖可口可乐小杯」和表里的「中杯」只差杯型
    core = re.sub(r"(小|中|大)杯$", "", name)
    if core != name:
        for size in ("中杯", "大杯", "小杯", ""):
            if core + size in table:
                return {**table[core + size], "match": "variant"}
    for key in table:
        if _squash(key) == _squash(name):
            return {**table[key], "match": "variant"}
    for key in table:
        k, n = _squash(key), _squash(name)
        if len(n) >= 2 and n in k:
            return {**table[key], "match": "fuzzy"}
    return None


def _squash(text: str) -> str:
    return re.sub(r"[\s·・()（）\-—_/]", "", str(text)).lower()


def _menu_yuan(value) -> float | None:
    """query-meals 的价格已经是元（字符串），不能再除 100；
    只有 calculate-price 是分。这两套单位混用是实测最容易翻车的地方。"""
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _yuan(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value) / CENTS, 2)
    except (TypeError, ValueError):
        return None


def _num(value) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def find_local_menu_file() -> Path:
    return Path(__file__).resolve().parent.parent / ".cache" / "menu.json"
