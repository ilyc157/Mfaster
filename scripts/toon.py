"""TOON 表解析。

麦当劳的营养接口不返回 JSON 数组，而是 TOON 文本：

    [160]{productName,energyKj,energyKcal,...}:
      猪柳麦满分,null,1288,308,16,16,24,781,213
      猪柳蛋麦满分,null,1618,387,23,21,25,846,243

行数声明 [160] 只是提示，实测会缺项，所以按实际行数走，不按声明数校验。
"""

from __future__ import annotations


def parse_toon_table(text: str) -> list[dict]:
    if not text:
        return []
    rows: list[dict] = []
    header: list[str] | None = None
    for raw in str(text).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("[") and "]{" in stripped:
            start = stripped.index("{")
            end = stripped.rindex("}")
            header = [c.strip() for c in _split(stripped[start + 1 : end])]
            continue
        if header is None:
            continue
        cells = _split(stripped)
        if not cells:
            continue
        if len(cells) < len(header):
            cells = cells + [""] * (len(header) - len(cells))
        elif len(cells) > len(header):
            # 末列是长文本时偶尔带逗号，合并回去
            head = cells[: len(header) - 1]
            tail = ",".join(cells[len(header) - 1 :])
            cells = head + [tail]
        rows.append(dict(zip(header, [c.strip() for c in cells])))
    return rows


def _split(line: str) -> list[str]:
    out, buf = [], []
    in_quote = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            if in_quote and i + 1 < len(line) and line[i + 1] == '"':
                buf.append('"')
                i += 2
                continue
            in_quote = not in_quote
            i += 1
            continue
        if ch == "," and not in_quote:
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf or out:
        out.append("".join(buf))
    return out
