"""命令行入口。

    find-store   找门店 / 查地址
    plan         报单 -> 配餐 -> 核价 -> 卡片
    edit         一句话改单（改完重出卡）
    order        确认后下单
    show         看存档摘要

模型只需要调这几条命令，不要自己去拼参数调 MCP 工具。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import card as card_mod
import menu as menu_mod
import planner
import roster as roster_mod
from mcd_client import McdError

LAST_CONTEXT = Path(__file__).resolve().parent.parent / "temp" / "last_context.json"

MEALS = [
    ("早餐", 7 * 60, 10 * 60 + 30),
    ("午餐", 10 * 60 + 30, 14 * 60 + 30),
    ("下午茶", 14 * 60 + 30, 17 * 60),
    ("晚餐", 17 * 60, 21 * 60),
    ("宵夜", 21 * 60, 24 * 60),
]


# ------------------------------------------------------------------ 餐段与时间


def _now_parts() -> tuple[int, int, int]:
    from mcd_client import server_now

    info = server_now()
    text = str(info.get("formatted") or "")
    try:
        date, clock = text.split(" ")
        hh, mm = clock.split(":")[:2]
        return int(hh), int(mm), int(date.split("-")[0])
    except Exception:
        import datetime as dt

        n = dt.datetime.now()
        return n.hour, n.minute, n.year


def current_meal() -> str:
    hh, mm, _ = _now_parts()
    minutes = hh * 60 + mm
    for name, start, end in MEALS:
        if start <= minutes < end:
            return name
    return "早餐"


def resolve_time(meal: str | None) -> tuple[str, str | None]:
    """返回（餐段名, reservationDate）。当前餐段不传预约时间。"""
    want = meal or current_meal()
    if want not in [m[0] for m in MEALS]:
        want = current_meal()
    import datetime as dt

    now = dt.datetime.now()
    minutes = now.hour * 60 + now.minute
    start, end = next(((s, e) for n, s, e in MEALS if n == want), (0, 0))
    if start <= minutes < end:
        return want, None
    target = now.date() if minutes < start else now.date() + dt.timedelta(days=1)
    # 预约时间必须落在目标餐段内部（餐段开始 +30 分钟），否则拿到的是上一餐段的菜单
    pick = dt.datetime.combine(target, dt.time(hour=0, minute=0)) + dt.timedelta(minutes=start + 30)
    return want, pick.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------- 取门店


def _load_last_context() -> dict | None:
    """上次点餐的门店与取餐方式。第二次点餐起，不问也知道送到哪。"""
    try:
        if LAST_CONTEXT.exists():
            return json.loads(LAST_CONTEXT.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _save_last_context(ctx: menu_mod.Context, place: str) -> None:
    try:
        LAST_CONTEXT.parent.mkdir(parents=True, exist_ok=True)
        LAST_CONTEXT.write_text(
            json.dumps(
                {
                    "store": ctx.store or {},
                    "order_type": ctx.order_type,
                    "be_type": ctx.be_type,
                    "be_code": ctx.be_code,
                    "address_id": ctx.address_id,
                    "place": place or "",
                    "meal": "",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _nearest(stores: list[dict]) -> dict:
    rows = list(stores)
    rows.sort(key=lambda s: s.get("distance") or 0)
    return rows[0]


def resolve_address(args) -> dict | None:
    """这单送到哪。None 表示到店自取。

    账号里的地址就是默认值：只有一个直接用，多个必须让人选。
    地址选错的代价不是多花几块钱，是饭送到另一个区、一个中午全废——所以这里宁可停下。
    """
    if args.order_type == 1:
        return None
    if getattr(args, "address_text", None):
        return {"addressId": "", "text": args.address_text.strip(), "contact": ""}
    rows = []
    try:
        rows = menu_mod.addresses()
    except McdError:
        rows = []
    if args.address_id:
        hit = next((r for r in rows if str(r.get("addressId")) == str(args.address_id)), None)
        return hit or {"addressId": args.address_id, "text": args.place or "", "contact": ""}
    if getattr(args, "address_pick", None):
        idx = int(args.address_pick)
        if idx < 1 or idx > len(rows):
            raise McdError(
                "BAD_ARGS",
                f"--address-pick {idx} 越界：账号里有 {len(rows)} 个地址",
                tool="find-store --addresses",
            )
        print(f"用账号地址 {idx}：{rows[idx - 1]['text']}")
        return rows[idx - 1]
    if not rows:
        if args.order_type == 2:
            raise McdError(
                "NEED_ADDRESS",
                "账号里没有配送地址：用 --address-text \"地址\" --city 城市 手填，或 --order-type 1 --be-type 1 到店取",
                tool="find-store --addresses",
            )
        return None
    if len(rows) == 1:
        print(f"用账号地址：{rows[0]['text']}")
        return rows[0]
    listed = "\n".join(
        f"{i}) {r['text']}" + (f"（{r['contact']}）" if r.get("contact") else "")
        for i, r in enumerate(rows, 1)
    )
    raise McdError(
        "MULTI_ADDRESS",
        f"账号里有 {len(rows)} 个配送地址，必须让人选：\n{listed}\n"
        "选第几个用 --address-pick N；堂食到店取用 --order-type 1 --be-type 1；手填地址用 --address-text \"…\" --city 城市",
        tool="find-store --addresses",
    )


def pick_store(args, addr: dict | None = None) -> dict:
    # 有配送地址 → 门店由地址决定，不能再用 --store 指定（外送门店必须能送这个地址）
    if addr and addr.get("addressId"):
        stores = menu_mod.open_stores(menu_mod.delivery_stores(str(addr["addressId"]), 2))
        if not stores:
            raise McdError(
                "NO_STORE",
                f"这个地址超出配送范围：{addr.get('text')}",
                tool="delivery-query-stores",
            )
        store = _nearest(stores)
        print(f"送到 {addr.get('text')} → {store.get('storeName')}")
        return store
    if addr and addr.get("text") and not args.store:
        if not (args.city and (args.keyword or addr.get("text"))):
            raise McdError(
                "BAD_ARGS",
                "手填地址要带 --city（地址里的城市），脚本用它找能送这片的外送门店",
                tool="find-store",
            )
        keyword = args.keyword or addr["text"]
        stores = menu_mod.open_stores(menu_mod.find_stores(args.city, keyword, be_type=2))
        if not stores:
            raise McdError("NO_STORE", f"{args.city}/{keyword} 附近没有在营业的外送门店", tool="query-nearby-stores")
        return _nearest(stores)
    if args.store:
        return {
            "storeCode": args.store,
            "storeName": args.store_name or args.store,
            "address": args.store_addr or "",
            "beCode": args.be_code,
        }
    if args.city and args.keyword:
        stores = menu_mod.open_stores(menu_mod.find_stores(args.city, args.keyword, be_type=args.be_type))
        if not stores:
            raise McdError("NO_STORE", f"{args.city}/{args.keyword} 附近没有在营业的门店", tool="query-nearby-stores")
        stores.sort(key=lambda s: s.get("distance") or 0)
        return stores[0]
    # 没说去哪吃 → 沿用上次。第一次用、没有历史，才需要问。
    last = _load_last_context()
    if last and (last.get("store") or {}).get("storeCode"):
        store = dict(last["store"])
        store.setdefault("beCode", last.get("be_code"))
        for key, val in (("order_type", "order-type"), ("be_type", "be-type"), ("address_id", "address-id")):
            if not getattr(args, key) and last.get(key):
                setattr(args, key, last[key])
        if not args.place and last.get("place"):
            args.place = last["place"]
        print(f"沿用上次：{store.get('storeName')}（要换店就给 --city/--keyword 或 --store）")
        return store
    raise McdError(
        "BAD_ARGS",
        "没说在哪家店吃，也没有上次记录：给 --city/--keyword 或 --store 一次，以后就不用再给了",
        tool="find-store",
    )


# ------------------------------------------------------------------------ plan


def cmd_find_store(args) -> int:
    if args.address_id:
        rows = menu_mod.delivery_stores(args.address_id, args.be_type)
        for row in rows[: args.limit]:
            print(f"- {row.get('storeCode')} {row.get('storeName')}  beCode={row.get('beCode')}")
        return 0
    if args.addresses:
        for i, row in enumerate(menu_mod.addresses(), 1):
            print(f"{i}) {row.get('text')}" + (f"（{row.get('contact')}）" if row.get("contact") else ""))
        return 0
    rows = menu_mod.find_stores(args.city, args.keyword, be_type=args.be_type)
    for row in rows[: args.limit]:
        flag = "营业" if row.get("businessStatus") is not False else "打烊"
        print(
            f"- {row.get('storeCode')} {row.get('storeName')} | {flag} | "
            f"{row.get('distance')}m | {row.get('address')} | beCode={row.get('beCode') or ''}"
        )
    return 0


def cmd_addresses(args) -> int:
    rows = menu_mod.addresses()
    if args.json:
        print(
            json.dumps(
                {
                    "count": len(rows),
                    "auto": len(rows) == 1,
                    "items": [
                        {
                            "idx": i,
                            "addressId": r.get("addressId"),
                            "text": r.get("text"),
                            "contact": r.get("contact", ""),
                            "phone": r.get("phone_masked", ""),
                        }
                        for i, r in enumerate(rows, 1)
                    ],
                },
                ensure_ascii=False,
                indent=1,
            )
        )
        return 0
    if not rows:
        print("账号里没有配送地址：用 --address-text \"地址\" --city 城市 手填，或 --order-type 1 到店取")
        return 0
    for i, r in enumerate(rows, 1):
        tail = f"　{r['contact']} {r.get('phone_masked', '')}".rstrip() if r.get("contact") else ""
        print(f"{i}) {r.get('text')}{tail}")
    if len(rows) > 1:
        print("有多个地址，必须让用户选：--address-pick N；堂食到店取 --order-type 1；手填 --address-text")
    return 0


def _build(args, roster: roster_mod.Roster, ctx: menu_mod.Context, menu: menu_mod.Menu, swaps: dict) -> dict:
    opts = planner.Options(
        budget=args.budget,
        note=args.note or "",
        meal=getattr(args, "meal_final", None) or (args.meal or ""),
        swaps=swaps,
        no_party=bool(getattr(args, "no_party", False)),
        extra={"place": args.place or ""},
    )
    return planner.build(roster, ctx, menu, opts)


def cmd_plan(args) -> int:
    args.out = args.out or "temp/plan.json"
    text = _read_roster(args)
    roster = _parse_roster(text, args)
    meal, reservation = resolve_time(args.meal)

    addr = resolve_address(args)
    if addr:
        args.order_type, args.be_type = 2, args.be_type or 2
        args.address_id = addr.get("addressId") or ""
        args.place = args.place or addr.get("text") or ""
    else:
        args.order_type, args.be_type = args.order_type or 1, args.be_type or 1

    store = pick_store(args, addr)
    ctx = menu_mod.Context(
        store_code=str(store.get("storeCode")),
        order_type=args.order_type,
        be_type=args.be_type,
        be_code=store.get("beCode") or args.be_code,
        address_id=args.address_id,
        reservation=reservation,
        store=store,
    )
    if args.order_type == 2 and not ctx.address_id:
        raise McdError("BAD_ARGS", "外送必须给 --address-id（先跑 find-store --addresses 查）", tool="delivery-query-addresses")

    menu = menu_mod.load_menu(ctx)
    args.meal_final = meal
    plan = _build(args, roster, ctx, menu, {})
    _save_and_render(plan, args, roster)
    _save_last_context(ctx, args.place or "")
    return 0


def cmd_edit(args) -> int:
    args.out = args.out or args.plan
    state = _load_plan(args.plan)
    roster = roster_mod.Roster(
        people=[
            roster_mod.Person(
                name=p["name"],
                items=[
                    roster_mod.Item(name=i["name"], qty=int(i.get("qty") or 1), note=i.get("note") or "", intent=i.get("intent"))
                    for i in p.get("items") or []
                ],
                spoken=p.get("spoken") or "",
                avoid=list(p.get("avoid") or []),
                light=bool(p.get("light")),
            )
            for p in state["_roster"]["people"]
        ],
        budget=state["_roster"].get("budget"),
    )
    swaps = dict(state.get("_swaps") or {})
    ctx_data = state["context"]
    store = ctx_data.get("store") or {}
    ctx = menu_mod.Context(
        store_code=str(store.get("storeCode") or ""),
        order_type=int(ctx_data.get("order_type") or 1),
        be_type=int(ctx_data.get("be_type") or 1),
        be_code=ctx_data.get("be_code"),
        address_id=ctx_data.get("address_id"),
        reservation=ctx_data.get("reservation"),
        store=store,
        take_way_code=ctx_data.get("take_way"),
        take_way_title=ctx_data.get("take_way_title"),
    )
    changes: list[str] = []
    args.meal_final = args.meal or ctx_data.get("meal") or ""

    if args.add:
        extra = roster_mod.parse(args.add, me_name=None)
        for p in extra.people:
            old = next((x for x in roster.people if x.name == p.name), None)
            if old:
                old.items.extend(p.items)
                changes.append(f"{p.name} 追加 {len(p.items)} 项")
            else:
                roster.people.append(p)
                changes.append(f"新增 {p.name}")

    if args.del_person:
        before = len(roster.people)
        roster.people = [p for p in roster.people if p.name not in args.del_person]
        changes.append(f"去掉 {'、'.join(args.del_person)}（{before - len(roster.people)} 人）")

    if args.set:
        name, _, body = args.set.partition("=") if "=" in args.set else (args.set.split()[0], "", " ".join(args.set.split()[1:]))
        target = next((p for p in roster.people if p.name == name.strip()), None)
        if target is None:
            raise McdError("BAD_ARGS", f"名单里没有「{name}」", tool="edit --set")
        parsed = roster_mod.parse(f"{name}：{body}")
        target.items = parsed.people[0].items if parsed.people else []
        changes.append(f"{name} 改成 {len(target.items)} 项")

    if args.qty:
        name, _, num = args.qty.partition("=")
        target = next((p for p in roster.people if p.name == name.strip()), None)
        if target and target.items:
            target.items[0].qty = max(1, int(num or 1))
            changes.append(f"{name} 数量改为 {target.items[0].qty}")

    if args.swap:
        name, _, idx = args.swap.partition("=")
        name, idx = name.strip(), int(idx or 1)
        person = next((p for p in state.get("people") or [] if p["name"] == name), None)
        if person is None or not person.get("items"):
            raise McdError("BAD_ARGS", f"名单里没有「{name}」", tool="edit --swap")
        alts = person["items"][0].get("alts") or []
        if not alts:
            raise McdError("BAD_ARGS", f"{name} 那一项没有可换项", tool="edit --swap")
        if idx > len(alts) or idx < 1:
            raise McdError("BAD_ARGS", f"{name} 只有 {len(alts)} 个可换项", tool="edit --swap")
        alt = alts[idx - 1]
        if not alt.get("round"):
            # 整份替换：把报单里这个人的第一项直接换成卡片上的候选套餐
            target = next((p for p in roster.people if p.name == name), None)
            if target is None or not target.items:
                raise McdError("BAD_ARGS", f"名单里没有「{name}」", tool="edit --swap")
            swaps.pop(f"{name}|0", None)
            target.items[0] = roster_mod.Item(name=alt["label"], qty=target.items[0].qty)
            changes.append(f"{name} 整份换成{alt['label']}")
        else:
            swaps[f"{name}|0"] = {alt["round"]: alt["code"]}
            changes.append(f"{name} 换成{alt['label']}")

    plan = _build(args, roster, ctx, menu_mod.load_menu(ctx), swaps)
    plan["_roster"] = roster.as_dict()
    plan["_swaps"] = swaps
    if args.budget:
        plan["context"]["budget"] = args.budget
    if args.place:
        plan["context"]["place"] = args.place
    _save_and_render(plan, args, roster, changes=changes)
    return 0


def i_name(person, item) -> str:
    return item.name if item else ""


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cmd_order(args) -> int:
    state = _load_plan(args.plan)
    ctx_data = state["context"]
    store = ctx_data.get("store") or {}
    items = []
    for person in state["people"]:
        for item in person["items"]:
            if not item.get("code"):
                continue
            line = {"productCode": item["code"], "quantity": int(item["qty"])}
            if item.get("roundList"):
                line["roundList"] = item["roundList"]
            items.append(line)
    if not items:
        raise McdError("EMPTY", "存档里没有可下单的商品", tool="create-order")

    order_type = int(ctx_data.get("order_type") or 1)
    payload = {
        "storeCode": str(store.get("storeCode")),
        "orderType": order_type,
        "beType": int(ctx_data.get("be_type") or 1),
        "items": items,
        "needTableware": bool(args.tableware),
    }
    if order_type == 1:
        payload["takeWayCode"] = ctx_data.get("take_way") or "eat-in"
    else:
        payload["addressId"] = ctx_data.get("address_id")
        if ctx_data.get("be_code"):
            payload["beCode"] = ctx_data["be_code"]
        if args.remark:
            payload["remark"] = args.remark[:50]
    if ctx_data.get("reservation"):
        payload["reservationDate"] = ctx_data["reservation"]

    if not args.yes:
        print("将要下这一单，确认没问题再加 --yes：")
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 3

    data = menu_mod.mcd_client.call("create-order", payload, ttl=0, timeout=90)
    urls = [v for v in _walk_values(data) if isinstance(v, str) and v.startswith("http")]
    print("已创建订单")
    order_no = None
    for key in ("orderId", "orderNo", "orderNumber"):
        if isinstance(data, dict) and data.get(key):
            order_no = str(data[key])
            print(f"订单号 {order_no}")
            break
    pay_url = urls[0] if urls else ""
    if pay_url:
        print(f"支付链接 {pay_url}")
    # 订单结果落盘：链接要能回查，不能只活在终端输出里。
    # 踩过：只渲染不存订单号，想再要一次链接只能重新下单（再扣一次钱）。
    order_row = {
        "order_no": order_no,
        "pay_url": pay_url,
        "created_at": now_stamp(),
        "payable": (state.get("summary") or {}).get("payable"),
        "store": ((state.get("context") or {}).get("store") or {}).get("storeName"),
        "raw": data,
    }
    order_path = Path(args.plan).with_name("order.json")
    order_path.write_text(json.dumps(order_row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ORDER={order_path}")

    print("把支付链接原样发给用户，手机点开就能付。链接 15 分钟内有效，也可去 App「我的订单」付款。")
    return 0


def _walk_values(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_values(v)
    else:
        yield obj


def cmd_show(args) -> int:
    state = _load_plan(args.plan)
    s = state.get("summary") or {}
    ctx = state.get("context") or {}
    store = ctx.get("store") or {}
    print(f"{store.get('storeName', '')} | {ctx.get('meal', '')} | {s.get('people')} 人 | 应付 {s.get('payable'):.2f}")
    for person in state.get("people") or []:
        names = "、".join(f"{i['name']}×{i.get('qty', 1)}" for i in person["items"])
        print(f"  {person['name']}: {names} | {person.get('subtotal'):.2f}")
    for flag in state.get("flags") or []:
        print(f"! {flag}")
    return 0


# -------------------------------------------------------------------- 公共部分


def _read_roster(args) -> str:
    if getattr(args, "roster_text", None):
        return args.roster_text
    if getattr(args, "roster", None):
        path = Path(args.roster)
        if not path.exists():
            raise McdError("NO_FILE", f"找不到报单文件 {path}", tool="plan")
        return path.read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        if data:
            return data
    raise McdError("BAD_ARGS", "没有报单内容：给 --roster 文件，或用 --roster-text", tool="plan")


def _parse_roster(text: str, args) -> roster_mod.Roster:
    text = text.strip()
    if text.startswith("[") or text.startswith("{"):
        return roster_mod.from_json(text)
    return roster_mod.parse(text, me_name=getattr(args, "me", None))


def _save_and_render(plan: dict, args, roster: roster_mod.Roster, changes: list[str] | None = None) -> None:
    plan.setdefault("_roster", roster.as_dict())
    plan.setdefault("_swaps", {})
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    print(_brief(plan, changes))
    print(f"PLAN={out}")
    # 只有一种卡片：会话内单屏片段。不再出长卡 HTML / PNG 截图。
    # 卡片是要嵌进对话直接看的，截图那一步既慢又会被窗口宽度切边。
    if args.widget:
        widget_path = Path(args.widget)
        widget_path.parent.mkdir(parents=True, exist_ok=True)
        widget_path.write_text(card_mod.render_compact(plan), encoding="utf-8")
        print(f"WIDGET={widget_path}")


def _brief(plan: dict, changes: list[str] | None) -> str:
    s = plan.get("summary") or {}
    ctx = plan.get("context") or {}
    lines = []
    if changes:
        lines.append("改动：" + "；".join(changes))
    lines.append(
        f"{s.get('people')} 人 {s.get('item_count')} 件 · 应付 ¥{s.get('payable', 0):.2f} · 人均 ¥{s.get('avg', 0):.2f}"
        + (f" · 原价 ¥{s.get('product_price', 0):.2f}" if s.get("discount") else "")
    )
    for person in plan.get("people") or []:
        brief_items = "、".join(
            f"{i['name']}" + (f"×{i['qty']}" if (i.get("qty") or 0) > 1 else "")
            + (f"({i['parts'][0]['name']}…)" if i.get("parts") else "")
            for i in person["items"]
        )
        note = ""
        bad = [x for i in person["items"] for x in (i.get("issues") or [])]
        if bad:
            note = f"  ⚠ {len(bad)} 项待确认"
        lines.append(f"  {person['name']}: {brief_items} ¥{person.get('subtotal', 0):.2f}{note}")
    for flag in plan.get("flags") or []:
        lines.append(f"! {flag}")
    for warn in (plan.get("warnings") or [])[:3]:
        lines.append(f"· {warn}")
    return "\n".join(lines)


def _load_plan(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise McdError("NO_FILE", f"找不到存档 {p}", tool="show")
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- 参数表


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mcd-order-card", description="麦当劳拼单确认卡")
    ap.add_argument("--no-cache", action="store_true", help="清掉本地菜单缓存重新拉")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("find-store", help="找门店/查地址")
    f.add_argument("--city")
    f.add_argument("--keyword")
    f.add_argument("--be-type", type=int, default=1)
    f.add_argument("--address-id", help="按地址查可配送门店")
    f.add_argument("--addresses", action="store_true", help="列出已存配送地址")
    f.add_argument("--limit", type=int, default=5)
    f.set_defaults(func=cmd_find_store)

    a = sub.add_parser("addresses", help="看账号里的配送地址")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_addresses)

    def add_plan_args(p, editing=False):
        p.add_argument("--store", help="门店 storeCode；不给就用 --city/--keyword 选最近的")
        p.add_argument("--store-name")
        p.add_argument("--store-addr")
        p.add_argument("--city")
        p.add_argument("--keyword")
        p.add_argument("--be-code")
        p.add_argument("--order-type", type=int, choices=[1, 2], help="1 到店 / 2 外送；不留给外送（账号有地址时），无地址才到店")
        p.add_argument("--be-type", type=int, choices=[1, 2, 5, 6], help="不留给到店取，有上次记录就沿用")
        p.add_argument("--address-id", help="已有配送地址的 addressId")
        p.add_argument("--address-pick", type=int, help="用账号里第几个地址（1 起），账号只有一个时不用传")
        p.add_argument("--address-text", help="手动填写送达地址（账号里没有的那个）")
        p.add_argument("--place", help="送到哪，写进卡片抬头")
        p.add_argument("--meal", choices=[m[0] for m in MEALS])
        p.add_argument("--budget", type=float)
        p.add_argument("--no-party", action="store_true", dest="no_party", help="不做多人餐合并，各点各的")
        p.add_argument("--note", help="整单备注，如 无接触配送")
        p.add_argument("--out")
        p.add_argument("--widget", help="会话内单屏卡片片段，唯一的卡片输出")
        if editing:
            p.add_argument("--plan", required=True)
        return p

    p = add_plan_args(sub.add_parser("plan", help="报单 -> 卡片"))
    p.add_argument("--roster", help="报单文本文件")
    p.add_argument("--roster-text", help="直接给报单文本")
    p.add_argument("--me", help="文本里的「我」是谁")
    p.set_defaults(func=cmd_plan)

    e = add_plan_args(sub.add_parser("edit", help="一句话改单"), editing=True)
    e.add_argument("--add", help='如 "王五：麦香鱼2"')
    e.add_argument("--del-person", nargs="+", dest="del_person")
    e.add_argument("--set", help='如 "张三=麦香鱼 中薯条"')
    e.add_argument("--qty", help='如 "张三=2"')
    e.add_argument("--swap", help='如 "张三=1"，换成该项第 1 个可换项')
    e.add_argument("--me")
    e.set_defaults(func=cmd_edit)

    o = sub.add_parser("order", help="确认后下单")
    o.add_argument("--plan", required=True)
    o.add_argument("--yes", action="store_true")
    o.add_argument("--remark")
    o.add_argument("--tableware", action="store_true")
    o.set_defaults(func=cmd_order)

    s = sub.add_parser("show", help="看存档摘要")
    s.add_argument("--plan", required=True)
    s.set_defaults(func=cmd_show)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "no_cache", False):
        from mcd_client import clear_cache

        print(f"已清缓存 {clear_cache()} 条")
    try:
        return args.func(args)
    except McdError as exc:
        print(f"ERROR {exc.code}: {exc.message}")
        if exc.hint:
            print(f"HINT {exc.hint}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
