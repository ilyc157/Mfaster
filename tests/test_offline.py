"""离线断言：不需要 token、不联网。python3 tests/test_offline.py"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import card  # noqa: E402
import mcd_client  # noqa: E402
import menu  # noqa: E402
import planner  # noqa: E402
import roster  # noqa: E402
import toon  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


# ---------------------------------------------------------------- 1. 报单解析


def test_roster() -> None:
    print("\n[1] 报单解析")
    text = """【20:31】@张三 板烧鸡腿堡套餐 x1，不太饿
李四：巨无霸 + 中薯 + 可乐
王五 麦辣鸡腿堡套餐，再来个1+1随心配
@赵六 随便
孙七 2个麦香鱼 1份薯条
我 麦乐鸡10块 大家分
预算 200"""
    r = roster.parse(text, me_name="林可")
    names = [p.name for p in r.people]
    check("六个人都认出来了", names == ["张三", "李四", "王五", "赵六", "孙七", "林可"], str(names))
    check("预算不落在人头上", r.budget == 200.0, str(r.budget))

    by = {p.name: p for p in r.people}
    check("巨无霸没被吃掉", [i.name for i in by["李四"].items] == ["巨无霸", "中薯", "可乐"],
          str([i.name for i in by["李四"].items]))
    check("王五认出来了", [i.name for i in by["王五"].items] == ["麦辣鸡腿堡套餐", "1+1随心配"],
          str([i.name for i in by["王五"].items]))
    check("随便标记为 intent", by["赵六"].items[0].intent == "any")
    check("数量 2 个麦香鱼", by["孙七"].items[0].qty == 2, str(by["孙七"].items[0].qty))
    check("麦乐鸡 10 块数量", by["林可"].items[0].qty == 10)
    check("不太饿识别为轻食", by["张三"].light is True)

    r2 = roster.parse("张三：不要辣，来份麦辣鸡腿堡套餐 x2")
    check("忌口辣", r2.people[0].avoid == ["辣"], str(r2.people[0].avoid))
    check("数量取后缀", r2.people[0].items[0].qty == 2)

    r3 = roster.parse("我 板烧鸡腿堡套餐，不要可乐要无糖的", me_name="林可")
    check("整段备注挂到前一项", "无糖" in (r3.people[0].items[0].note or ""), r3.people[0].items[0].note)

    r4 = roster.parse("赵六：我不吃牛肉，随便点")
    check("忌口牛肉", "牛肉" in r4.people[0].avoid, str(r4.people[0].avoid))
    check("随便点算泛称", r4.people[0].items and r4.people[0].items[0].intent == "any",
          str([(i.name, i.intent) for i in r4.people[0].items]))

    r5 = roster.parse("两个人：张三李四都要巨无霸")
    check("多人挤一行会告警", any("好几个人" in w for w in r5.warnings), str(r5.warnings))


# ------------------------------------------------------------------ 2. TOON


def test_toon() -> None:
    print("\n[2] TOON 表")
    text = "[2]{productName,energyKcal,sodium}:\n  巨无霸,513,957\n  麦香鱼,325,462"
    rows = toon.parse_toon_table(text)
    check("解出两行", len(rows) == 2, str(rows))
    check("字段对齐", rows[0]["productName"] == "巨无霸" and rows[0]["energyKcal"] == "513", str(rows[0]))
    rows2 = toon.parse_toon_table('[1]{a,b}:\n  "带,逗号",2')
    check("引号内逗号不切", rows2[0]["a"] == "带,逗号", str(rows2))


# ------------------------------------------------------------- 3. 混合文本解包


def test_find_json() -> None:
    print("\n[3] 混合文本解包")
    text = '说明文字 {不是:json}\n{"success":true,"code":200,"data":{"a":1}}\n## 后面还有 markdown'
    got = mcd_client.find_json(text)
    check("取出正文 JSON", got == {"success": True, "code": 200, "data": {"a": 1}}, str(got))
    check("空文本返回 None", mcd_client.find_json("") is None)


# ------------------------------------------------------------------ 4. 价格


def test_price_units() -> None:
    print("\n[4] 价格单位")
    check("菜单是元", menu._menu_yuan("34.5") == 34.5, str(menu._menu_yuan("34.5")))
    check("核价是分", menu._yuan(3450) == 34.5, str(menu._yuan(3450)))
    check("缺失不报错", menu._menu_yuan(None) is None and menu._yuan("") is None)


# ------------------------------------------------------------- 5. 名字对齐


def _dish(code, name, price, cat="精选单人餐", tags=None):
    return menu.Dish(code=code, name=name, price=price, original=price, category=cat, tags=tags or [])


def test_rank() -> None:
    print("\n[5] 名字对齐")
    dishes = [
        _dish("1", "板烧鸡腿堡", 23.5),
        _dish("2", "板烧鸡腿堡三件套", 34.5),
        _dish("3", "巨无霸", 25.5),
        _dish("4", "麦乐鸡", 14.0, "炸鸡"),
        _dish("5", "麦乐鸡10块", 42.0, "炸鸡"),
    ]
    top = planner.rank("板烧鸡腿堡套餐", dishes)
    check("说套餐就给套餐", top and top[0][1].name == "板烧鸡腿堡三件套", str([(s, d.name) for s, d in top[:2]]))
    top2 = planner.rank("板烧", dishes)
    check("说简称给单品", top2 and top2[0][1].name == "板烧鸡腿堡", str([(s, d.name) for s, d in top2[:2]]))
    check("别名双吉能展开", planner.expand("双吉") == "双层吉士汉堡")

    class P:
        name = "x"
        avoid = []
        spoken = ""

    item = roster.Item(name="麦乐鸡", qty=10)
    packed = planner._pack_item(item, dishes)
    check("10 块识别为规格", packed is not None and packed[0].name == "麦乐鸡10块" and packed[1].qty == 1,
          str(packed and (packed[0].name, packed[1].qty)))


# ------------------------------------------------------------------ 6. 卡片


def _plan() -> dict:
    return {
        "context": {
            "store": {"storeName": "同方信息港餐厅"},
            "place": "3 楼会议室",
            "order_type": 1,
            "take_way_title": "堂食",
            "meal": "晚餐",
            "budget": 100,
            "note": "",
        },
        "summary": {"people": 1, "item_count": 1, "payable": 34.5, "product_price": 34.5,
                    "discount": 0, "fees": 0, "avg": 34.5, "kcal_total": 680},
        "people": [{
            "name": "张三", "subtotal": 34.5, "kcal": 680, "kcal_complete": True,
            "items": [{
                "name": "板烧鸡腿堡三件套", "qty": 1, "unit": 34.5, "subtotal": 34.5,
                "kcal": 680, "kcal_partial": False,
                "parts": [
                    {"round": "1", "title": "板烧鸡腿堡", "name": "板烧鸡腿堡", "qty": 1},
                    {"round": "2", "title": "选择套餐内小食", "name": "中薯条", "qty": 1},
                    {"round": "3", "title": "选择套餐内饮料", "name": "可乐中杯", "qty": 1},
                ],
                "why": "你的原话是「板烧套餐」，含有主食+小食+饮料。",
                "alts": [{"label": "大薯条", "diff": 2.5, "round": "2", "code": "9", "kcal": 390}],
                "issues": [], "adjust": [], "note": "", "share": False,
            }],
        }],
        "shared": [],
        "flags": ["比预算多 ¥5.00"],
        "warnings": [],
    }


def test_card() -> None:
    print("\n[6] 卡片渲染（单屏）")
    html = card.render_compact(_plan())
    for token in ("张三", "板烧鸡腿堡三件套", "中薯条", "可乐中杯", "¥34.50", "3 楼会议室", "晚餐", "人均"):
        check(f"卡片含「{token}」", token in html)
    check("片段无 html 骨架", "<!doctype" not in html and "<style>" in html)
    check("套餐拆在同一行", "板烧鸡腿堡 · 中薯条 · 可乐中杯" in html)
    check("需要拍板的事露出来", "比预算多" in html)
    check("长卡专属内容不再渲染", "可以换" not in html and "依据" not in html and "kcal" not in html)

    over_plan = _plan()
    over_plan["context"]["budget"] = 30
    check("超预算标出来", "超预算" in card.render_compact(over_plan))
    check("超出的金额算得对", "¥4.50" in card.render_compact(over_plan))

    warn_plan = _plan()
    warn_plan["people"][0]["items"][0]["issues"] = ["「大份」按大薯条算的"]
    check("有待确认项的人名后打红点", "color:#DA291C;font-weight:800" in card.render_compact(warn_plan))

    party_plan = _plan()
    party_plan["party"] = {"name": "畅享吃鸡三人餐", "need": 1, "cost": 86.0, "saved": 1.9,
                           "for": ["A", "B", "C"]}
    with_party = card.render_compact(party_plan)
    check("多人餐单独一行", "多人餐" in with_party and "畅享吃鸡三人餐" in with_party)
    check("多人餐写清省了多少", "省 ¥1.90" in with_party)
    check("没有待拍板的事就明说", "没有需要拍板的事" in card.render_compact({**_plan(), "flags": []}))


# ------------------------------------------------------------- 7. 预算补差


def test_cheapest_fix() -> None:
    print("\n[7] 预算补差")
    plan = _plan()
    people = plan["people"]
    people[0]["items"][0]["qty"] = 2
    people[0]["items"][0]["unit"] = 34.5
    people[0]["items"][0]["alts"] = [{"label": "中薯条", "diff": -3.0, "round": "2", "code": "8"}]
    text = planner._cheapest_fix(people, 5.0)
    check("给出具体换法", "（-¥" in text, text)
    check("一步补不上会给出总额", "合计" in text, text)
    text2 = planner._cheapest_fix(people, 3.0)
    check("一步能补上就只动一步", "-¥6.00" in text2 and "合计 -¥6.00" in text2, text2)  # 两份 × 3 元
    text3 = planner._cheapest_fix(people, 500.0)
    check("收不住会说还差多少", "仍差" in text3, text3)


# ------------------------------------------------------------------ 8. 存档


def test_plan_shape() -> None:
    print("\n[8] 存档结构")
    plan = _plan()
    blob = json.dumps(plan, ensure_ascii=False)
    check("可序列化", isinstance(blob, str))
    plan["context"]["budget"] = None
    check("预算可空", json.dumps(plan, ensure_ascii=False))


# ------------------------------------------------------- 9. 随便的整份候选


def test_default_dish() -> None:
    print("\n[9] 随便的整份候选")
    dishes = [
        _dish("1", "板烧鸡腿堡三件套", 34.5, "人气热卖"),
        _dish("2", "麦辣鸡腿堡三件套", 33.0, "人气热卖"),
        _dish("3", "巨无霸三件套", 38.5, "人气热卖"),
        _dish("4", "麦香鱼", 20.0, "单品"),
    ]

    class P:
        name = "赵六"
        avoid = []
        spoken = "随便"

    chosen, alts = planner._default_dish(P, dishes)
    check("主选是人气分类里最便宜的套餐", chosen.name == "麦辣鸡腿堡三件套", chosen.name)
    check("给了两个整份候选", len(alts) == 2, str([a["label"] for a in alts]))
    check("候选标了 whole", all(a.get("whole") for a in alts))
    check("差价是整份差价", alts[0]["diff"] == 1.5, str(alts[0]))
    check("候选 round 为空（走整份替换）", all(a["round"] is None for a in alts))

    class P2(P):
        avoid = ["辣"]

    chosen2, alts2 = planner._default_dish(P2, dishes)
    check("忌口辣的人不会拿到麦辣", "辣" not in chosen2.name, chosen2.name)


# --------------------------------------------------------- 10. 分餐速览


def test_handout() -> None:
    print("\n[10] 一人一行（分餐照读）")
    plan = _plan()
    plan["people"].append({
        "name": "李四", "subtotal": 20.0,
        "items": [{"name": "麦香鱼", "qty": 1, "subtotal": 20.0, "parts": [], "issues": []}],
    })
    plan["summary"].update(people=2, item_count=2, payable=54.5, product_price=54.5, avg=27.25)
    html = card.render_compact(plan)
    check("每人一行", html.count('class="mc-row"') == 2, str(html.count('class="mc-row"')))
    check("人名在行首", ">张三<" in html and ">李四<" in html)
    check("金额跟着自己那行", html.count('class="mc-amt"') == 2)
    check("一人一份，不用再翻明细", "板烧鸡腿堡 · 中薯条 · 可乐中杯" in html)

    shared = json.loads(json.dumps(plan))
    shared["people"][0]["items"][0]["party_for"] = ["张三", "李四", "王五"]
    shared["people"][0]["items"][0]["qty"] = 1
    html2 = card.render_compact(shared)
    check("合买的项标人数", "3 人共用" in html2)

    split = json.loads(json.dumps(plan))
    split["people"][1]["items"][0] = {
        "name": "与张三共用「板烧鸡腿堡三件套」", "qty": 0, "subtotal": 27.25,
        "parts": [], "issues": [], "split_of": "板烧鸡腿堡三件套",
    }
    html3 = card.render_compact(split)
    check("分摊行不显示数量", 'class="mc-muted"' in html3)


# --------------------------------------------------------- 11. 下单只给链接


def test_order_link() -> None:
    print("\n[11] 下单只出链接，不出二维码")
    import cli

    parser = cli.build_parser()
    args = parser.parse_args(["order", "--plan", "temp/plan.json", "--yes"])
    check("order 没有 --checkout 参数", not hasattr(args, "checkout"))
    plan_args = parser.parse_args(["plan", "--roster-text", "张三 随便"])
    check("plan 不再有 --html", not hasattr(plan_args, "html"))
    check("plan 不再有 --png", not hasattr(plan_args, "png"))
    check("plan 仍支持 --widget", hasattr(plan_args, "widget"))
    check("二维码模块已移除", not (ROOT / "scripts" / "qrcodegen.py").exists())
    check("支付卡模块已移除", not (ROOT / "scripts" / "checkout.py").exists())


# --------------------------------------------------------- 12. 沿用上次


def test_last_context() -> None:
    print("\n[12] 门店沿用")
    import cli

    ctx = menu.Context(
        store_code="1420762",
        order_type=1,
        be_type=1,
        store={"storeCode": "1420762", "storeName": "同方信息港餐厅"},
    )
    old = None
    if cli.LAST_CONTEXT.exists():
        old = cli.LAST_CONTEXT.read_text(encoding="utf-8")
    try:
        cli._save_last_context(ctx, "3 楼会议室")
        got = cli._load_last_context()
        check("写完能读回", got and got["store"]["storeCode"] == "1420762", str(got))
        check("地点也记住了", got.get("place") == "3 楼会议室")

        class A:
            store = None
            store_name = None
            store_addr = None
            be_code = None
            city = None
            keyword = None
            order_type = None
            be_type = None
            address_id = None
            place = None

        store = cli.pick_store(A())
        check("没参数时沿用上次门店", store["storeCode"] == "1420762", str(store))
    finally:
        if old is None:
            cli.LAST_CONTEXT.unlink(missing_ok=True)
        else:
            cli.LAST_CONTEXT.write_text(old, encoding="utf-8")


# ------------------------------------------------------- 13. 口味不能配错


def test_flavor_routing() -> None:
    """真实跑出来的错：王总要「辣的汉堡」，第一版给了双层吉士（不辣）。
    根因是 expand 把「汉堡」当别名替换掉，辣味信号中途丢失。"""
    print("\n[13] 口味与肉类路由")
    dishes = [
        _dish("1", "麦辣鸡腿汉堡", 22.5, "鸡肉汉堡/卷"),
        _dish("2", "麦辣鸡腿堡四件套", 28.0, "鸡肉汉堡/卷"),
        _dish("3", "双层吉士汉堡", 23.0, "巨无霸牛鱼肉堡"),
        _dish("4", "芝士安格斯厚牛堡", 31.5, "安格斯MAX厚牛堡"),
        _dish("5", "牛肉堡单品精选", 15.0, "巨无霸牛鱼肉堡"),
        _dish("6", "板烧鸡腿堡", 23.5, "鸡肉汉堡/卷"),
    ]
    top = planner.rank("辣的汉堡", dishes)
    check("要辣的不会给成不辣的", top and "辣" in top[0][1].name, str([(round(s, 2), d.name) for s, d in top[:2]]))

    top2 = planner.rank("牛肉汉堡", dishes)
    check("要牛肉先给字面带牛的", top2 and top2[0][1].name == "芝士安格斯厚牛堡",
          str([(round(s, 2), d.name) for s, d in top2[:3]]))
    check("「单品精选」不排在前面", all(d.name != "牛肉堡单品精选" for _s, d in top2[:1]),
          str([d.name for _s, d in top2[:2]]))

    top3 = planner.rank("麦辣鸡腿汉堡套餐", dishes)
    check("说套餐优先给套餐", top3 and "套" in top3[0][1].name, str([(round(s, 2), d.name) for s, d in top3[:2]]))


# --------------------------------------------------- 14. 随便的份量分层


def test_default_portion() -> None:
    print("\n[14] 随便不选错份量")
    dishes = [
        _dish("1", "精选超值随心配", 13.9, "精选单人餐"),
        _dish("2", "板烧鸡腿堡三件套", 34.5, "精选单人餐"),
        _dish("3", "鱼牛汉堡四件套随心选", 32.0, "精选单人餐"),
        _dish("4", "巨无霸三件套", 38.5, "精选单人餐"),
    ]

    class P:
        name = "某同事"
        avoid = []
        spoken = "随便"

    chosen, alts = planner._default_dish(P, dishes)
    check("正餐优先于 1+1 随心配", chosen.name != "精选超值随心配", chosen.name)
    check("三件套优先于四件套", "四件套" not in chosen.name, chosen.name)
    picks = {chosen.name} | {a["label"] for a in alts}
    check("轮换给出不同套餐", len(picks) >= 3, str(picks))

    chosen1, _ = planner._default_dish(P, dishes, offset=1)
    check("不同的人拿到不同主选", chosen1.name != chosen.name, f"{chosen.name} vs {chosen1.name}")


# ------------------------------------------------------------ 15. 地址决策


def _args(**kw):
    import argparse

    base = {"address_text": None, "address_id": None, "address_pick": None, "order_type": None, "place": ""}
    base.update(kw)
    return argparse.Namespace(**base)


def test_address_decision() -> None:
    print("\n[15] 地址决策（默认用账号地址）")
    import cli

    orig = menu.addresses
    try:
        menu.addresses = lambda: [{"addressId": "a1", "text": "甲地 1 号", "contact": "李生"}]
        got = cli.resolve_address(_args())
        check("只有一个地址就直接用，不问", got and got["addressId"] == "a1", str(got))

        menu.addresses = lambda: [
            {"addressId": "a1", "text": "甲地 1 号", "contact": "李生"},
            {"addressId": "a2", "text": "乙地 2 号", "contact": "林可"},
        ]
        err = None
        try:
            cli.resolve_address(_args())
        except mcd_client.McdError as exc:
            err = exc
        check("多个地址必须让人选", err is not None and err.code == "MULTI_ADDRESS", str(err))
        check("报错里列出每个地址", err is not None and "甲地 1 号" in err.message and "乙地 2 号" in err.message)
        check("报错里给出堂食和手填的路子", err is not None and "--order-type 1" in err.message and "--address-text" in err.message)

        got = cli.resolve_address(_args(address_pick=2))
        check("按用户选的序号取地址", got and got["addressId"] == "a2", str(got))

        got = cli.resolve_address(_args(address_text="新地址 3 号"))
        check("手填地址优先于账号地址", got and got["text"] == "新地址 3 号" and got["addressId"] == "")

        check("说了堂食就不要地址", cli.resolve_address(_args(order_type=1)) is None)

        menu.addresses = lambda: []
        err = None
        try:
            cli.resolve_address(_args(order_type=2))
        except mcd_client.McdError as exc:
            err = exc
        check("没有地址又要求外送时报错", err is not None and err.code == "NEED_ADDRESS", str(err))
        check("没地址时不传 order-type 就退到店取", cli.resolve_address(_args()) is None)
    finally:
        menu.addresses = orig


# ------------------------------------------------------------ 16. 多人餐合并


def test_party_merge() -> None:
    print("\n[16] 多人餐合并")
    dishes = [
        _dish("p1", "畅享吃鸡三人餐", 86.0, "多人分享餐"),
        _dish("p2", "鸡牛堡堡双人餐", 64.9, "多人分享餐"),
        _dish("p3", "超级翅桶", 110.0, "炸鸡"),
        _dish("s1", "板烧鸡腿堡三件套", 30.0, "人气热卖"),
    ]
    opts = planner._party_options(dishes)
    check("三人餐识别为 3 人份", opts and opts[0][1] == 3, str([(d.name, n) for d, n in opts]))
    check("纯翅桶不参与（没有主食）", all("翅桶" not in d.name for d, _n in opts))

    def person(name, code, price, any_=True):
        return {
            "name": name,
            "all_any": any_,
            "items": [{"code": code, "name": "板烧鸡腿堡三件套", "qty": 1, "subtotal": price}],
            "subtotal": price,
            "kcal": 0,
        }

    people = [person("A", "s1", 30.0), person("B", "s1", 30.0), person("C", "s1", 30.0), person("D", "s1", 25.0, False)]
    lines = [{"productCode": "s1", "quantity": 1, "_who": p["name"]} for p in people]
    new_lines, party = planner._merge_party(people, dishes, lines, None, {"p1": None, "p2": None})
    check("三个「随便」的合成一份三人餐", party is not None and party["need"] == 1, str(party))
    check("省下的钱算对", party is not None and party["saved"] == 4.0, str(party and party["saved"]))
    check("有明确需求的人不参与合并", party is not None and party["for"] == ["A", "B", "C"], str(party and party["for"]))
    check("下单行只提交一份多人餐", len([l for l in new_lines if l.get("productCode") == "p1"]) == 1)
    check("原来那三个人的单人套餐行被移除", len([l for l in new_lines if l.get("_who") in ("A", "B", "C")]) == 1)
    check("每人分摊后相加等于整份", round(sum(p["subtotal"] for p in people[:3]), 2) == 86.0, str([p["subtotal"] for p in people]))

    # 合买是脚本替用户做的决定，必须每人留一条「各点各的」退路
    a_alts = people[0]["items"][0].get("alts") or []
    check("合买的人有「各点」备选", len(a_alts) == 1 and a_alts[0].get("pre") == "各点", str(a_alts))
    check("退路写的是他原本要点的那份", a_alts and a_alts[0]["label"] == "板烧鸡腿堡三件套", str(a_alts))
    check("差价＝各点价−分摊价", a_alts and abs(a_alts[0]["diff"] - 1.33) < 0.01, str(a_alts and a_alts[0]["diff"]))
    check("搭档也有退路", bool((people[1]["items"][0].get("alts") or [])), str(people[1]["items"][0]))
    check("没合买的人不加这条", not (people[3]["items"][0].get("alts") or []), str(people[3]["items"][0]))


def test_party_not_worth() -> None:
    print("\n[16b] 不够便宜就不合并")
    dishes = [_dish("p1", "畅享吃鸡三人餐", 120.0, "多人分享餐")]
    people = [
        {"name": n, "all_any": True, "items": [{"code": "s1", "qty": 1, "subtotal": 25.0}], "subtotal": 25.0, "kcal": 0}
        for n in ("A", "B", "C")
    ]
    lines = [{"productCode": "s1", "quantity": 1, "_who": p["name"]} for p in people]
    new_lines, party = planner._merge_party(people, dishes, lines, None, {"p1": None})
    check("更贵就各点各的", party is None, str(party))
    check("原样保留三条行", len(new_lines) == 3)

    only_one = [{"name": "A", "all_any": True, "items": [{"code": "s1", "qty": 1, "subtotal": 25.0}], "subtotal": 25.0, "kcal": 0}]
    _l, party2 = planner._merge_party(only_one, dishes, [{"productCode": "s1", "quantity": 1, "_who": "A"}], None, {})
    check("只有一个人时不合并", party2 is None)


# ------------------------------------------------------------ 16c. 取餐方式


def test_take_way() -> None:
    print("\n[16c] 取餐方式不会写错")
    ctx = menu.Context(store_code="1", order_type=2, be_type=2)
    planner._attach_take_way([], {"take_way": []}, ctx)
    check("外送单不会写成「到店自取」", ctx.take_way_title == "麦乐送", str(ctx.take_way_title))

    ctx2 = menu.Context(store_code="1", order_type=1, be_type=1)
    planner._attach_take_way([], {"take_way": [{"code": "eat-in", "title": "堂食"}]}, ctx2)
    check("到店单用接口返回的取餐方式", ctx2.take_way_title == "堂食", str(ctx2.take_way_title))


# ------------------------------------------------------------ 17. 单屏卡片

def _compact_plan():
    return {
        "context": {
            "store": {"storeName": "麦当劳深圳科技园餐厅"},
            "place": "科苑南路2688号 云栖花园 3 栋 502",
            "order_type": 2,
            "take_way_title": "麦乐送",
            "meal": "宵夜",
            "budget": None,
            "reservation": None,
        },
        "summary": {"people": 3, "item_count": 3, "payable": 146.5, "avg": 48.83, "product_price": 137.5},
        "party": {"name": "畅享吃鸡三人餐", "need": 1, "cost": 86.0, "saved": 1.9, "for": ["A", "B", "C"]},
        "people": [
            {
                "name": "张子涵",
                "subtotal": 26.0,
                "items": [{"name": "高达吉士双牛堡", "qty": 1, "parts": [], "issues": []}],
            },
            {
                "name": "同事A",
                "subtotal": 28.67,
                "items": [
                    {
                        "name": "畅享吃鸡三人餐",
                        "qty": 1,
                        "parts": [{"name": "麦辣鸡腿汉堡"}, {"name": "板烧鸡腿堡"}, {"name": "中薯条"}, {"name": "麦乐鸡5块"}, {"name": "可乐"}],
                        "party_for": ["A", "B", "C"],
                        "issues": [],
                    }
                ],
            },
            {
                "name": "同事B",
                "subtotal": 28.67,
                "items": [{"name": "与同事A共用「畅享吃鸡三人餐」", "qty": 0, "split_of": "同事A", "parts": [], "issues": []}],
            },
        ],
        "flags": [],
        "warnings": [],
    }


def test_compact() -> None:
    print("\n[17] 会话内单屏卡片")
    html = card.render_compact(_compact_plan())
    check("走单屏版式", 'class="mc"' in html)
    check("不再输出发群用的长卡", 'class="sheet"' not in html)
    check("金额与人均都在", "146.50" in html and "人均" in html)
    check("一人一行", html.count('class="mc-row"') == 3, str(html.count('class="mc-row"')))
    check("套餐拆开写", "板烧鸡腿堡" in html)
    check("超长组合截断", "…" in html, html[html.find("mc-dish") : html.find("mc-dish") + 200])
    check("多人餐标记", "3 人共用" in html and "省" in html)
    check("共用的人不重复计价", "mc-muted" in html)
    check("没有内联 style 之外的脚本", "<script" not in html)


def test_alts() -> None:
    print("\n[18] 每人备选")
    plan = _compact_plan()
    # 有明确需求的人：两个同类候选，编号必须和 `edit --swap "名字=N"` 的 N 对齐
    plan["people"][0]["items"][0]["alts"] = [
        {"label": "培根芝士双牛堡", "diff": 1.0, "round": None, "code": "9", "whole": True},
        {"label": "巨无霸", "diff": 6.5, "round": None, "code": "10", "whole": True},
    ]
    # 合买的人：只有一条「各点各的」退路，靠 pre 前缀说清楚
    plan["people"][1]["items"][0]["alts"] = [
        {"label": "麦辣鸡腿堡三件套", "diff": 2.4, "round": None, "code": "11", "whole": True, "pre": "各点"},
    ]
    html = card.render_compact(plan)
    check("备选行出现", html.count('class="mc-alt"') == 2, str(html.count('class="mc-alt"')))
    check("第一个备选印成「备选1」", "<i>备选1</i>培根芝士双牛堡" in html)
    check("第二个备选印成「备选2」", "<i>备选2</i>巨无霸" in html)
    check("不再用「换 1 2」这种黑话", "换 <" not in html)
    check("差价写明", "+¥1.00" in html and "+¥6.50" in html)
    check("合买的人有「各点」退路", "各点麦辣鸡腿堡三件套" in html)
    check("没备选的人不占一行空行", html.count('class="mc-alt"') == 2)

    # 同价与降价写法
    same = _compact_plan()
    same["people"][0]["items"][0]["alts"] = [{"label": "板烧鸡腿堡", "diff": 0, "round": None, "code": "1"}]
    check("同价写同价", "同价" in card.render_compact(same))
    down = _compact_plan()
    down["people"][0]["items"][0]["alts"] = [{"label": "汉堡包", "diff": -10.0, "round": None, "code": "2"}]
    check("降价用负号", "−¥10.00" in card.render_compact(down))

    # 备选最多三条：多出来的不挤占单屏空间
    many = _compact_plan()
    many["people"][0]["items"][0]["alts"] = [
        {"label": f"候选{i}", "diff": i, "round": None, "code": str(i)} for i in range(1, 5)
    ]
    h = card.render_compact(many)
    check("备选最多三条", "候选3" in h and "候选4" not in h)


def test_combo_qty() -> None:
    print("\n[19] 套餐轮次份数（60006 的真凶）")
    # 真实返回：双人餐饮料轮 minQuantity=2，但默认选项上写 quantity=1。
    # 照抄会在核价时报「商品不可售」，看着像门店缺货，其实只是份数不够。
    drink = menu.Round(
        round="3", title="选择套餐内饮料", min_qty=2, max_qty=2,
        choices=[menu.Choice(code="520689", name="小杯鲜萃咖啡", default=True, diff=0.0, quantity=1)],
    )
    snack = menu.Round(
        round="2", title="选择套餐内小食", min_qty=2, max_qty=2,
        choices=[menu.Choice(code="904825", name="脆薯饼", default=True, diff=0.0, quantity=2)],
    )
    one = menu.Round(
        round="4", title="选择酱", min_qty=1, max_qty=1,
        choices=[menu.Choice(code="901089", name="优品豆浆大杯", default=True, diff=0.0, quantity=1)],
    )
    combo = menu.Combo(code="9900013805", name="随心选麦满分双人餐", rounds=[snack, drink, one])
    rl = {r["round"]: r["comboItemList"][0]["quantity"] for r in combo.round_list()}
    check("饮料轮按下限补齐两杯", rl.get("3") == 2, str(rl))
    check("本来就对的轮次不动", rl.get("2") == 2, str(rl))
    check("单人轮保持一份", rl.get("4") == 1, str(rl))
    parts = {p["round"]: p["qty"] for p in combo.default_parts()}
    check("卡片拆解也用补齐后的份数", parts.get("3") == 2, str(parts))

    # 单人套餐（min=1）不受影响
    solo = menu.Combo(
        code="x", name="板烧三件套",
        rounds=[menu.Round(round="2", title="小食", min_qty=1, max_qty=1,
                           choices=[menu.Choice(code="1", name="中薯条", default=True, diff=0.0, quantity=1)])],
    )
    check("单人套餐份数不变", solo.round_list()[0]["comboItemList"][0]["quantity"] == 1)


def main() -> int:
    test_roster()
    test_toon()
    test_find_json()
    test_price_units()
    test_rank()
    test_card()
    test_cheapest_fix()
    test_plan_shape()
    test_default_dish()
    test_handout()
    test_order_link()
    test_last_context()
    test_flavor_routing()
    test_default_portion()
    test_address_decision()
    test_party_merge()
    test_party_not_worth()
    test_take_way()
    test_compact()
    test_alts()
    test_combo_qty()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
