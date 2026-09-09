"""SKILL.md / 命令 markdown 的 frontmatter 解析（YAML 极简子集，够用即可）。

复刻 claude skills 的 SKILL.md 头部约定，但只取本引擎需要的字段子集：
``key: value`` 标量 + ``key:`` 后跟 ``  - item`` 列表。解析宽容：没有
frontmatter / ``---`` 缺闭合 / 坏行一律退回"全文当正文"，不因手写错误崩掉
加载器（技能文件是第三方/用户手写的，宁丢字段不丢加载）。
"""
from __future__ import annotations

import re

_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")
_LIST_ITEM_RE = re.compile(r"^\s*-\s+(.*)$")

_FALSE_VALUES = {"false", "no", "0", "off", ""}


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """拆出 (frontmatter dict, 正文)。

    key 一律小写；标量存 str，列表行累加成 list[str]。``name`` 若缺失，调用方
    用自己的名字回退（描述同理）。正文去掉前导空行。
    """
    if not text.startswith("---"):
        return {}, text
    # 取第二段 "---" 为闭合符；缺失则视为没有 frontmatter（整文件当正文）
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    head, body = parts[1], parts[2]

    data: dict[str, object] = {}
    cur_key: str | None = None
    for line in head.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        list_item = _LIST_ITEM_RE.match(line)
        if list_item and cur_key is not None:
            existing = data.get(cur_key)
            if not isinstance(existing, list):
                data[cur_key] = []
            assert isinstance(data[cur_key], list)
            data[cur_key].append(list_item.group(1).strip())
            continue
        scalar = _KEY_RE.match(line)
        if scalar:
            cur_key = scalar.group(1).lower()
            val = scalar.group(2).strip()
            # 值可能带引号（如 version: '1.2'），剥掉成对引号
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            data[cur_key] = val
    return data, body.lstrip("\n")


def fm_str(data: dict[str, object], key: str, default: str = "") -> str:
    val = data.get(key)
    return str(val) if isinstance(val, str) and val else default


def fm_str_list(data: dict[str, object], key: str) -> tuple[str, ...]:
    val = data.get(key)
    if isinstance(val, list):
        out = [str(v).strip() for v in val if str(v).strip()]
        return tuple(out)
    if isinstance(val, str):
        # "a, b" 或 "a b" 单行形式都认
        items = [p for p in re.split(r"[\s,]+", val) if p]
        return tuple(items)
    return ()


def fm_bool(data: dict[str, object], key: str, default: bool = False) -> bool:
    val = data.get(key)
    if not isinstance(val, str) or not val:
        return default
    return val.strip().lower() not in _FALSE_VALUES


def fm_first_line(text: str, fallback: str) -> str:
    """frontmatter 没写 description 时，回退到正文第一行（粗提炼）。"""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-", "|", ">", "```")):
            return line
    return fallback
