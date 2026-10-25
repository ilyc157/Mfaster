"""拼单确认卡的渲染。

只有一种卡片：嵌进对话、一屏看完、一人一行。
不出长卡，也不截图——截图会被窗口宽度切边，还多一步等待。

改版式改这里，不要在对话里手写 HTML。
"""

from __future__ import annotations

import html

COMPACT_CSS = """
.mc{width:100%;max-width:920px;margin:0 auto;background:#fff;color:#181818;border:1px solid #EDEAE6;
  border-radius:14px;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",sans-serif;
  font-size:18px;line-height:1.55;-webkit-font-smoothing:antialiased;}
.mc-hd{background:#DA291C;color:#fff;padding:16px 20px 14px;}
.mc-line{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;}
.mc-brand{font-size:24px;font-weight:800;letter-spacing:.4px;}
.mc-way{font-size:16px;opacity:.92;border:1px solid rgba(255,255,255,.55);border-radius:999px;padding:1px 10px;}
.mc-total{margin-left:auto;font-size:30px;font-weight:800;font-variant-numeric:tabular-nums;}
.mc-sub{display:flex;gap:16px;flex-wrap:wrap;margin-top:7px;font-size:16px;opacity:.93;}
.mc-sub .mc-place{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.mc-body{padding:4px 20px;}
.mc-row{display:flex;align-items:flex-start;gap:12px;padding:9px 0;border-bottom:1px solid #F3F0EC;}
.mc-row:last-child{border-bottom:0;}
.mc-who{flex:0 0 5.4em;font-size:19px;font-weight:700;line-height:1.45;}
.mc-dish{flex:1;min-width:0;}
.mc-dish b{font-size:18px;font-weight:600;line-height:1.45;}
.mc-dish b + b::before{content:"· ";color:#B8B8B8;font-weight:400;}
.mc-dish i{display:block;font-style:normal;font-size:16px;color:#6A6A6A;line-height:1.45;margin-top:2px;}
.mc-amt{flex:0 0 auto;font-size:18px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.45;}
.mc-tag{display:inline-block;margin-left:8px;background:#FFF3C4;color:#8A5A00;font-size:16px;
  border-radius:6px;padding:0 8px;font-weight:600;vertical-align:1px;}
.mc-alt{display:block;font-size:16px;color:#3A3A3A;line-height:1.5;margin-top:3px;}
.mc-alt i{font-style:normal;color:#8A8A8A;font-weight:600;margin-right:5px;}
.mc-alt s{text-decoration:none;margin-right:16px;}
.mc-alt span.d{color:#8A5A00;margin-left:6px;}
.mc-muted{color:#8A8A8A;font-weight:500;}
.mc-ft{border-top:1px solid #EDEAE6;background:#FBF8F3;padding:12px 20px 14px;font-size:16px;}
.mc-party{color:#7A4A00;background:#FFF6D8;border-radius:8px;padding:6px 10px;margin-bottom:8px;}
.mc-flag{color:#B3261E;line-height:1.5;}
.mc-hint{color:#7A7A7A;margin-top:8px;line-height:1.5;}
"""


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def money(value) -> str:
    if value is None:
        return "—"
    try:
        return f"¥{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _diff_text(diff) -> str:
    """差价：同价、加钱、省钱三种，符号比文字快。"""
    try:
        d = float(diff or 0)
    except (TypeError, ValueError):
        return ""
    if abs(d) < 0.01:
        return "同价"
    return f"{'+' if d > 0 else '−'}¥{abs(d):.2f}"


def _alt_line(items: list[dict]) -> str:
    """备选行：写成「备选1 / 备选2」，用户嘴上怎么说，卡上就怎么印。

    编号取的是这个人第一项（items[0]）的备选，与 cli.py 的 --swap 「名字=N」一一对应；
    合并成多人餐的人没有套餐内备选，但有一条「各点」——不提就换不出来。
    """
    if not items:
        return ""
    alts = items[0].get("alts") or []
    if not alts:
        return ""
    chips = []
    for idx, alt in enumerate(alts[:3], start=1):
        label = (alt.get("pre") or "") + (alt.get("label") or "")
        diff = _diff_text(alt.get("diff"))
        chips.append(
            f"<s><i>备选{idx}</i>{esc(label)}"
            + (f' <span class="d">{diff}</span>' if diff else "")
            + "</s>"
        )
    return f'<span class="mc-alt">{" ".join(chips)}</span>'


def _dish_cell(item: dict) -> str:
    """一行之内把「什么·怎么配的」说完。套餐必须拆开，否则等于没说清吃什么。"""
    name = item.get("name") or "未匹配"
    if item.get("split_of"):
        return f'<b class="mc-muted">{esc(name)}</b>'
    qty = item.get("qty") or 1
    tag = ""
    party = item.get("party_for") or []
    if len(party) > 1:
        tag = f'<span class="mc-tag">{len(party)} 人共用</span>'
    head = f'<b>{esc(name)}</b>' + (f'<span class="mc-tag">×{qty}</span>' if qty > 1 else "") + tag
    parts = [p.get("name") for p in (item.get("parts") or []) if p.get("name")]
    tail_text = " · ".join(parts[:4]) + (" …" if len(parts) > 4 else "")
    tail = f"<i>{esc(tail_text)}</i>" if tail_text else ""
    return head + tail


def render_compact(plan: dict) -> str:
    """单屏卡片：一屏看完，一人一行。

    只解决「现在要不要下单」——谁、吃什么、多少钱，加上真正需要拍板的事。
    搭配理由、热量不进来：看多了反而决定不了。但每人的备选要留，不留就只能重收一遍单。
    """
    ctx = plan.get("context") or {}
    s = plan.get("summary") or {}
    store = ctx.get("store") or {}
    people = plan.get("people") or []
    budget = ctx.get("budget")
    payable = float(s.get("payable") or 0)
    over = round(payable - float(budget), 2) if budget else 0.0

    place = ctx.get("place") or store.get("storeName") or "到店自取"
    way = ctx.get("take_way_title") or ("麦乐送" if int(ctx.get("order_type") or 1) == 2 else "到店自取")
    when = ctx.get("meal") or "当前"
    reservation = ctx.get("reservation") or ""
    if reservation:
        when += " " + reservation.split(" ")[-1]

    sub = [
        f'<span class="mc-place">{esc(place)}</span>',
        f"<span>{esc(when)}</span>",
        f"<span>人均 {money(s.get('avg'))}</span>",
        f"<span>{s.get('people', 0)} 人 {s.get('item_count', 0)} 件</span>",
    ]
    if over > 0:
        sub.append(f'<span style="font-weight:700">超预算 {money(over)}</span>')
    elif budget:
        sub.append("<span>预算内</span>")

    rows = []
    for person in people:
        items = person.get("items") or []
        cells = "".join(_dish_cell(item) for item in items)
        warn = any(item.get("issues") for item in items)
        dot = '<span style="color:#DA291C;font-weight:800"> ·</span>' if warn else ""
        rows.append(
            f'<div class="mc-row"><span class="mc-who">{esc(person.get("name"))}{dot}</span>'
            f'<span class="mc-dish">{cells}{_alt_line(items)}</span>'
            f'<span class="mc-amt">{money(person.get("subtotal"))}</span></div>'
        )

    foot = []
    party = plan.get("party") or None
    if party:
        foot.append(
            f'<div class="mc-party">多人餐：{esc(party["name"])} ×{party.get("need", 1)} '
            f'{money(party.get("cost"))}　·　{len(party.get("for") or [])} 人合买，省 {money(party.get("saved"))}</div>'
        )
    for flag in (plan.get("flags") or [])[:2]:
        foot.append(f'<div class="mc-flag">！{esc(flag)}</div>')
    if not foot:
        foot.append('<div class="mc-party">本单没有需要拍板的事</div>')

    head_html = (
        f'<div class="mc-hd"><div class="mc-line"><span class="mc-brand">麦当劳 · 拼单</span>'
        f'<span class="mc-way">{esc(way)}</span>'
        f'<span class="mc-total">{money(payable)}</span></div>'
        f'<div class="mc-sub">{"".join(sub)}</div></div>'
    )
    return (
        f"<style>{COMPACT_CSS}</style>"
        f'<div class="mc">{head_html}<div class="mc-body">{"".join(rows)}</div>'
        f'<div class="mc-ft">{"".join(foot)}'
        f'<div class="mc-hint">要改就说一句：「张子涵 换成备选2」「李四不要了」「加个人，王五 麦香鱼」</div>'
        f"</div></div>"
    )
