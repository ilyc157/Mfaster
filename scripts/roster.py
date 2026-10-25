"""报单解析：群聊/口头/表格文本 -> 结构化名单。

这是唯一需要「猜」的一层，所以规则全部写成可读的表，猜不准就记一条 warning 交给上层问人，
绝不静默丢单。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

ME = "__ME__"

CN_NUM = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
UNITS = "个份杯盒套块只支袋瓶张碟碗"
QTY_SUFFIX = re.compile(r"[,，]?\s*(?:[xX×*]\s*(\d+)|(\d+)\s*[" + UNITS + r"]|([" + "".join(CN_NUM) + r"])\s*[" + UNITS + r"])\s*$")
QTY_PREFIX = re.compile(r"^(?:(\d+)\s*[" + UNITS + r"]|([xX×*])\s*(\d+)\s*|([" + "".join(CN_NUM) + r"])\s*[" + UNITS + r"])\s*")
BULLET = re.compile(r"^(?:[-*•·]|\d+[.、)）]|[（(]\d+[)）])\s*")
TIME_TAG = re.compile(r"^[【\[]?\d{1,2}[:：]\d{2}[】\]]?\s*")
SPEAKER_AT = re.compile(r"^@\s*([^\s:：,，]{1,12})\s*")
SPEAKER_COLON = re.compile(r"^([^\s:：,，]{1,12})\s*[:：]\s*")

# 备注只认受控词表。裸「无」会把「巨无霸」拆成「巨」+ 备注「无霸」，
# 泛化的「去X」会把「去冰可乐」整串吞掉，这两个坑都是实测踩出来的。
NOTE_PATTERNS = [
    r"(不要|不加|去掉?|少|多|加|换)(冰|糖|酱|菜|葱|洋葱|香菜|芝士|奶|番茄酱|酸黄瓜|生菜|吸管|餐具|辣椒|辣)",
    r"(无糖|去冰|少冰|多冰|常温|零度)",
    r"备注[:：]?\s*([^\s,，。；;]{1,20})",
    r"(不要辣|超辣|微辣)",
]

ANY_WORDS = {
    "随便", "都行", "都可以", "随意", "无所谓", "你定", "你们定", "你看着办", "看着办", "看着点",
    "不挑", "啥都行", "什么都行", "吃啥都行", "任意", "听你的", "听你们的", "随你", "都随你",
    "随你们", "我不挑", "都ok", "你选", "你们选", "你帮我选", "帮我选", "来啥吃啥", "给啥吃啥",
    "安排", "你安排", "随便安排", "看你们", "都成", "都中",
}

# 忌口：人级别约束，会参与筛选，也会写进卡片依据
DIET_WORDS = {
    "辣": ("辣", "麻辣", "香辣"),
    "牛肉": ("牛肉", "牛堡", "安格斯", "巨无霸"),
    "猪肉": ("猪", "培根", "烟肉"),
    "鸡肉": ("鸡",),
    "海鲜": ("鱼", "虾", "鳕", "蟹"),
    "香菜": ("香菜",),
    "奶": ("奶", "芝士", "冰淇淋", "新地", "麦旋风"),
    "咖啡": ("咖啡", "拿铁", "美式"),
    "冰": ("冰",),
}
AVOID_PAT = re.compile(r"(不吃|不要|不能吃|忌口?|过敏|不碰|讨厌)([^\s,，。；;、]{1,6})")
LIGHT_PAT = re.compile(r"(减肥|减脂|控卡|控热量|热量低|轻食|在瘦|怕胖|吃少|不太饿|不饿|少吃)")

GLOBAL_HINT = re.compile(r"(预算|人均|总共|一共|不要辣|都不吃|都要|团餐|聚餐|加班|开会)")

# 群聊里一定会出现的废话。判断标准是「不含任何食物字」，不是黑名单匹配，
# 否则「不太饿」「谢谢」会被当成一道菜塞进订单。
FOOD_CHARS = "堡薯鸡鱼可乐饮派咖啡茶饼卷饭汤肉奶冰辣腿翅块条杯堡沙律果"
CHATTER = {
    "不太饿", "不饿", "饿了", "谢谢", "辛苦", "辛苦了", "好的", "可以", "行", "ok", "OK",
    "同上", "一样", "同前", "老规矩", "大家分", "分着吃", "共享", "一起吃", "一起",
    "没了", "就这些", "先这样", "收到", "没问题", "都行吧",
}
PREFIX_STRIP = re.compile(r"^(再来|再要|还要|再来一|给我|帮我|麻烦|来|要|点|加|换|想吃|想喝|来一|来份|来个)+")



@dataclass
class Item:
    name: str
    qty: int = 1
    note: str = ""
    intent: str | None = None  # "any" 表示「随便」

    def as_dict(self) -> dict:
        out = {"name": self.name, "qty": self.qty}
        if self.note:
            out["note"] = self.note
        if self.intent:
            out["intent"] = self.intent
        return out


@dataclass
class Person:
    name: str
    items: list[Item] = field(default_factory=list)
    spoken: str = ""
    avoid: list[str] = field(default_factory=list)
    light: bool = False
    raw_lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "items": [i.as_dict() for i in self.items],
            "spoken": self.spoken,
            "avoid": self.avoid,
            "light": self.light,
            "raw_lines": self.raw_lines,
        }


@dataclass
class Roster:
    people: list[Person]
    warnings: list[str] = field(default_factory=list)
    budget: float | None = None

    @property
    def item_count(self) -> int:
        return sum(len(p.items) for p in self.people)

    def as_dict(self) -> dict:
        return {
            "people": [p.as_dict() for p in self.people],
            "count": len(self.people),
            "item_count": self.item_count,
            "budget": self.budget,
            "warnings": self.warnings,
        }


# ------------------------------------------------------------------------ 入口


def parse(text: str, *, me_name: str | None = None) -> Roster:
    people: list[Person] = []
    warnings: list[str] = []
    budget: float | None = None
    current: Person | None = None

    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = TIME_TAG.sub("", line)
        line = BULLET.sub("", line)
        line = line.strip()
        if not line:
            continue

        found = re.search(r"(?:总)?预算\s*[:：]?\s*(\d+(?:\.\d+)?)", line)
        if found:
            budget = float(found.group(1))
            line = line[: found.start()] + line[found.end():]
            line = line.strip()
            if not line:
                continue

        name, body = _split_speaker(line)
        if name:
            current = _get_person(people, name, me_name)
            current.raw_lines.append(raw.strip())
            if body:
                _absorb(current, body, warnings)
            continue

        # 没有说话人：第一行挂在「我」名下，其余接着上一个人
        if current is None:
            current = _get_person(people, ME, me_name)
        current.raw_lines.append(raw.strip())
        _absorb(current, line, warnings)

    people = [p for p in people if p.items or p.spoken]
    if not people:
        warnings.append("没解析出任何人和餐品，确认一下报单文本格式")

    for p in people:
        if len(p.items) > 6:
            warnings.append(f"{_disp(p.name)} 一个人点了 {len(p.items)} 项，确认是不是把别人的单也算进来了")
        if re.search(r"(和|、|都|大家|我们|[一二两三四五六七八九十\d]+[个位]?人|多个|几位)", p.name) or len(p.name) > 8:
            warnings.append(f"「{p.name}」看起来像一句话里报了好几个人，需要拆开确认")

    return Roster(people=people, warnings=warnings, budget=budget)


def from_json(text: str) -> Roster:
    """接受 [{name, items:[{name,qty,note}]}] 或 {"people":[...]} 两种结构。"""
    blob = json.loads(text)
    rows = blob.get("people") if isinstance(blob, dict) else blob
    people = []
    for row in rows or []:
        if isinstance(row, str):
            people.append(Person(name=row, items=[Item(name="随便", intent="any")]))
            continue
        name = str(row.get("name") or ME)
        items = []
        for it in row.get("items") or []:
            if isinstance(it, str):
                items.append(Item(name=it))
            else:
                items.append(
                    Item(
                        name=str(it.get("name") or ""),
                        qty=int(it.get("qty") or 1),
                        note=str(it.get("note") or ""),
                        intent=it.get("intent"),
                    )
                )
        people.append(
            Person(
                name=name,
                items=[i for i in items if i.name],
                spoken=str(row.get("spoken") or ""),
                avoid=[str(a) for a in (row.get("avoid") or [])],
                light=bool(row.get("light")),
            )
        )
    budget = None
    if isinstance(blob, dict) and blob.get("budget"):
        budget = float(blob["budget"])
    return Roster(people=people, budget=budget)


# --------------------------------------------------------------------- 内部


def _disp(name: str) -> str:
    return "你" if name == ME else name


def _get_person(people: list[Person], name: str, me_name: str | None) -> Person:
    if name in ("我", "本人", "自己"):
        name = me_name or ME
    for p in people:
        if p.name == name:
            return p
    p = Person(name=name)
    people.append(p)
    return p


def _split_speaker(line: str) -> tuple[str | None, str]:
    m = SPEAKER_AT.match(line)
    if m:
        return m.group(1), line[m.end():].strip()
    m = SPEAKER_COLON.match(line)
    if m:
        cand = m.group(1)
        if not _looks_like_dish(cand):
            return cand, line[m.end():].strip()
    # 「王五 麦辣鸡腿堡套餐」：空格前是个短名字，且本身不像菜
    parts = line.split(None, 1)
    if len(parts) == 2:
        head = parts[0]
        if (
            1 <= len(head) <= 6
            and not _looks_like_dish(head)
            and not head[0].isdigit()
            and not re.fullmatch(r"[xX×*]?\d+[" + UNITS + r"]?", head)
            and head not in CHATTER
        ):
            return head, parts[1].strip()
    return None, line


def _looks_like_dish(text: str) -> bool:
    keys = ("堡", "薯", "可乐", "鸡", "麦", "套餐", "饮", "派", "咖啡", "圣代", "新地", "随心")
    return any(k in text for k in keys)


def _looks_like_note(text: str) -> bool:
    return bool(re.search(r"(不要|不加|去掉?|少|多|加|换|无糖|去冰|少冰|常温|零度)", text or ""))


def _is_chatter(text: str) -> bool:
    """只认明确的废话，不靠「像不像菜名」猜——「巨无霸」不含任何食物字，但不是废话。"""
    t = re.sub(r"\s+", "", text or "")
    if not t:
        return True
    if t in CHATTER or t.lower() in CHATTER:
        return True
    if len(t) <= 6 and any(w in t for w in ("饿", "谢谢", "辛苦", "同上", "一样", "大家", "分着", "收到", "没了", "先这样")):
        return True
    if len(t) <= 8 and re.match(r"^我?(不吃|不要|不能吃|忌口?|过敏|不碰|讨厌)", t):
        return True
    return False


def _looks_like_food(text: str) -> bool:
    text = text or ""
    if any(ch in text for ch in FOOD_CHARS):
        return True
    if text.lower().replace(" ", "") in ANY_WORDS:
        return True
    return any(k in text for k in ("套餐", "早餐", "午餐", "随心", "组合", "拼盘", "大杯", "中杯", "小杯"))


def _absorb(person: Person, body: str, warnings: list[str]) -> None:
    if body:
        person.spoken = (person.spoken + " " + body).strip() if person.spoken else body

    for key, words in DIET_WORDS.items():
        for m in AVOID_PAT.finditer(body):
            phrase = m.group(2)
            if key in phrase or any(w in phrase for w in words):
                if key not in person.avoid:
                    person.avoid.append(key)
                break
    if LIGHT_PAT.search(body):
        person.light = True

    segments = [s for s in _segment(body) if s]
    if not segments:
        if person.items:
            return
        if any(w in body.lower() for w in ANY_WORDS):
            person.items.append(Item(name="随便", intent="any"))
        elif body:
            person.items.append(Item(name=body))
        return

    for seg in segments:
        item = _parse_item(seg)
        if item is None:
            continue
        if _is_chatter(item.name):
            # 「不要可乐要无糖的」这类整段其实是备注，挂到前一项上，别丢
            note = item.note or seg
            if person.items and _looks_like_note(note):
                person.items[-1].note = (person.items[-1].note + " " + note).strip()
            continue
        if item.intent == "any" and any(not i.intent for i in person.items):
            # 已经有具体餐品了，这句「随便」是语气，不当一单
            continue
        if item.intent == "any" and person.items and person.items[-1].intent == "any":
            person.items[-1].qty = max(person.items[-1].qty, item.qty)
            continue
        person.items.append(item)

    if len(segments) > 4:
        warnings.append(f"{_disp(person.name)} 那条拆出 {len(segments)} 段，检查一下有没有拆错")


def _segment(body: str) -> list[str]:
    text = body.strip()
    if not text:
        return []
    # 保护 1+1 这类词里的加号
    text = re.sub(r"(\d)\s*\+\s*(\d)", r"\1§\2", text)
    for sep in ("、", ",", "，", "/", "+", "；", ";", "|", "＋"):
        text = text.replace(sep, " ")
    text = text.replace("§", "+")

    raw_tokens = [t for t in re.split(r"\s+", text) if t]
    merged: list[str] = []
    for tok in raw_tokens:
        if merged and _is_qty_only(tok):
            merged[-1] = merged[-1] + tok
            continue
        if merged and re.fullmatch(r"[xX×*]", merged[-1][-1:]) and tok.isdigit():
            merged[-1] = merged[-1] + tok
            continue
        merged.append(tok)
    return merged


def _is_qty_only(tok: str) -> bool:
    if re.fullmatch(r"[xX×*]\s*\d+", tok):
        return True
    if re.fullmatch(r"\d+\s*[" + UNITS + r"]?", tok) and len(tok) <= 3:
        return True
    if re.fullmatch(r"[" + "".join(CN_NUM) + r"]\s*[" + UNITS + r"]", tok):
        return True
    return False


def _parse_item(seg: str) -> Item | None:
    text = seg.strip()
    if not text:
        return None

    note = ""
    for _ in range(3):
        hit = None
        for pat in NOTE_PATTERNS:
            m = re.search(pat, text)
            if m:
                hit = m
                break
        if not hit:
            break
        note = (note + " " + hit.group(0).strip()).strip()
        text = (text[: hit.start()] + text[hit.end():]).strip()

    qty = 1
    m = QTY_SUFFIX.search(text)
    if m:
        if m.group(1):
            qty = int(m.group(1))
        elif m.group(2):
            qty = int(m.group(2))
        elif m.group(3):
            qty = CN_NUM.get(m.group(3), 1)
        text = text[: m.start()].strip()
    else:
        m = QTY_PREFIX.match(text)
        if m:
            if m.group(1):
                qty = int(m.group(1))
            elif m.group(3):
                qty = int(m.group(3))
            elif m.group(4):
                qty = CN_NUM.get(m.group(4), 1)
            text = text[m.end():].strip()

    text = PREFIX_STRIP.sub("", text).strip()
    text = re.sub(r"^[来要点份个吃喝]+", "", text).strip()
    text = re.sub(r"[吧了呀啊哦嘛]$", "", text).strip()
    if not text:
        return None

    if text.lower().replace(" ", "") in ANY_WORDS:
        return Item(name="随便", qty=qty, note=note, intent="any")
    if _any_prefix(text):
        return Item(name="随便", qty=qty, note=note, intent="any")
    if text in CHATTER:
        return None
    return Item(name=text, qty=max(1, qty), note=note)


def _any_prefix(text: str) -> bool:
    core = re.sub(r"[点吧了呢啊呀哦~～。!！]+$", "", re.sub(r"\s+", "", text or ""))
    return core in ANY_WORDS
