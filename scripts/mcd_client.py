"""麦当劳 MCP 客户端。

只做三件事：拿 token、发 JSON-RPC、把返回的「人读文本外壳 + JSON 正文」拆成 dict。
业务语义一律不下沉到这里，交给 menu.py。

MCP 端点是无状态的：不需要先 initialize，也不需要回传 Mcp-Session-Id。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://mcp.mcd.cn"
WORKBUDDY_MCP = Path.home() / ".workbuddy" / "mcp.json"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"

# 只读工具才允许走本地缓存；写操作（下单/取消/领券）永远打真接口。
CACHEABLE = {
    "query-meals",
    "query-meal-detail",
    "query-nearby-stores",
    "delivery-query-stores",
    "delivery-query-addresses",
    "list-nutrition-foods",
    "query-store-coupons",
    "now-time-info",
}
DEFAULT_TTL = 300  # 秒；菜单 5 分钟内基本不变

_DECODER = json.JSONDecoder()


class McdError(RuntimeError):
    """接口层错误。code 为业务码或本地码（NO_TOKEN / HTTP_401 ...）。"""

    def __init__(self, code, message, *, tool=None, hint=None):
        self.code = str(code)
        self.message = str(message)
        self.tool = tool
        self.hint = hint
        tail = f"（{tool}）" if tool else ""
        super().__init__(f"[{self.code}] {self.message}{tail}")

    def as_dict(self):
        return {"code": self.code, "message": self.message, "tool": self.tool, "hint": self.hint}


# --------------------------------------------------------------------------- token


def resolve_token() -> str:
    """优先环境变量，其次读 WorkBuddy 里已配置的麦当劳连接器。"""
    for key in ("MCD_MCP_TOKEN", "MCD_TOKEN", "MCDONALDS_MCP_TOKEN"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()
    try:
        cfg = json.loads(WORKBUDDY_MCP.read_text(encoding="utf-8"))
        auth = cfg["mcpServers"]["mcdonald"]["headers"]["Authorization"]
        token = auth.split(" ", 1)[1].strip()
        if token:
            return token
    except Exception:
        pass
    raise McdError(
        "NO_TOKEN",
        "找不到麦当劳 MCP Token",
        hint="设置环境变量 MCD_MCP_TOKEN，或在 WorkBuddy 连接器里启用麦当劳 MCP",
    )


# ----------------------------------------------------------------- payload decode


def find_json(text: str):
    """从混合文本里取出第一个完整 JSON 对象。

    实测返回长这样：markdown 说明 + JSON + 有时还跟着一段 markdown。
    所以既不能 json.loads 整段，也不能从末尾倒着找。
    """
    if not text:
        return None
    starts = []
    for marker in ('{"success"', '{"code"', '{"data"'):
        idx = text.find(marker)
        if idx >= 0:
            starts.append(idx)
    if not starts:
        # 兜底：第一个可能是 JSON 的 '{'
        for i, ch in enumerate(text):
            if ch == "{" and i + 1 < len(text) and text[i + 1] in '"{[':
                starts.append(i)
                break
    if not starts:
        return None

    # 候选起点可能不止一个，取「解出来最大」的那个：正文 JSON 一定比说明文字里的花括号大
    best, best_size = None, -1
    for idx in sorted(set(starts)):
        try:
            obj, end = _DECODER.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if end > best_size:
            best, best_size = obj, end
    return best


def _join_text(result: dict) -> str:
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") in (None, "text"):
            parts.append(item.get("text") or "")
    return "".join(parts)


# ----------------------------------------------------------------------- cache


def _cache_path(tool: str, args: dict) -> Path:
    raw = tool + json.dumps(args, ensure_ascii=False, sort_keys=True)
    key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{tool}.{key}.json"


def _cache_get(tool: str, args: dict, ttl: int):
    if ttl <= 0 or tool not in CACHEABLE:
        return None
    path = _cache_path(tool, args)
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if time.time() - blob.get("t", 0) > ttl:
        return None
    return blob.get("data")


def _cache_put(tool: str, args: dict, data) -> None:
    if tool not in CACHEABLE:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(tool, args)
        path.write_text(json.dumps({"t": time.time(), "data": data}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def clear_cache() -> int:
    if not CACHE_DIR.exists():
        return 0
    n = 0
    for p in CACHE_DIR.glob("*.json"):
        try:
            p.unlink()
            n += 1
        except Exception:
            pass
    return n


# ------------------------------------------------------------------------ call


def call(tool: str, arguments: dict | None = None, *, ttl: int = DEFAULT_TTL, timeout: int = 60) -> dict:
    """调一个 MCP 工具，返回 data 字段（没有 data 就返回整个 payload）。"""
    args = arguments or {}
    cached = _cache_get(tool, args, ttl)
    if cached is not None:
        return cached

    payload = _post(tool, args, timeout)
    data = payload.get("data")
    result = data if data is not None else payload
    _cache_put(tool, args, result)
    return result


def _post(tool: str, args: dict, timeout: int) -> dict:
    token = resolve_token()
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("content-type", "")
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        code = exc.code
        hint = None
        if code == 401:
            hint = "Token 无效或已过期，去 open.mcd.cn/mcp 重新申请"
        elif code == 429:
            hint = "触发限流（600 次/分钟），等几秒再试"
        raise McdError(f"HTTP_{code}", f"接口返回 HTTP {code}", tool=tool, hint=hint) from exc
    except urllib.error.URLError as exc:
        raise McdError("NETWORK", f"连不上 {ENDPOINT}：{exc.reason}", tool=tool) from exc

    if "text/event-stream" in ctype:
        raw = _sse_to_json(raw)

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        obj = find_json(raw)
        if obj is None:
            raise McdError("BAD_RESPONSE", f"返回无法解析：{raw[:200]}", tool=tool)
        envelope = obj

    if isinstance(envelope, dict) and envelope.get("error"):
        err = envelope["error"]
        raise McdError(err.get("code", "RPC"), err.get("message", "未知错误"), tool=tool)

    result = (envelope or {}).get("result") or {}
    if result.get("isError"):
        raise McdError("TOOL_ERROR", _join_text(result)[:300] or "工具执行失败", tool=tool)

    payload = find_json(_join_text(result))
    if payload is None:
        if isinstance(result.get("structuredContent"), dict):
            payload = result["structuredContent"]
        else:
            raise McdError("EMPTY", "返回里没有 JSON 正文", tool=tool)

    code = payload.get("code")
    if code not in (None, 200, "200"):
        raise McdError(code, payload.get("message") or "业务返回失败", tool=tool, hint=_hint_for(code))
    return payload


def _sse_to_json(raw: str) -> str:
    """Streamable HTTP 可能回 SSE，只取最后一条 data。"""
    chunks = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            chunks.append(line[5:].strip())
    return chunks[-1] if chunks else raw


# 实测踩过的业务码 -> 该做什么
_HINTS = {
    "600047": "地址解析成功但周边没有门店，换 city 再试",
    "600050": "searchType 默认是搜收藏，传 2 才是搜附近",
    "600057": "门店已打烊，换一家 businessStatus=true 的",
    "600058": "city 和 keyword 必须同时给",
    "60006": "商品当前不可售，多半是没带 reservationDate，查的不是同一份菜单",
}


def _hint_for(code) -> str | None:
    return _HINTS.get(str(code))


def server_now() -> dict:
    """拿服务端时间（判餐段用），失败就退回本机时间。"""
    try:
        return call("now-time-info", {}, ttl=30)
    except McdError:
        import datetime as _dt

        now = _dt.datetime.now()
        return {"formatted": now.strftime("%Y-%m-%d %H:%M:%S"), "date": now.strftime("%Y-%m-%d"), "hour": now.hour}
