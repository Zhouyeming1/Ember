"""项目级 .claude/settings.json 的 permissions.allow/deny（工具级规则）加载。

对齐 claude 的 settings.json 语义（``permissions.allow`` / ``permissions.deny``
字符串数组），但**门控比 claude 严**：项目文件默认不信任是引擎既有口径
（hooks.json / permissions.json 都靠 ``EMBERPY_PROJECT_*`` env 门控才并入），
这里沿用——只有显式 ``EMBERPY_PROJECT_SETTINGS=1`` 才读
``<workspace>/.claude/settings.json``。

规则文法：``"Tool"``（该工具任意输入）或 ``"Tool(pattern)"``（输入前缀/通配匹配）。
Tool 可写引擎名（run_command/read_file/...）或 claude CamelCase
（Bash/Read/Write/Edit/WebSearch/WebFetch），别名解析与匹配在 policy.py。
坏 JSON / 缺文件一律静默（不炸 worker）；坏条目的丢弃也在 policy 构造时处理。

明确不做：permissions.defaultMode、用户级 ~/.claude/settings.json 合并、
allow 豁免敏感集合（.env/.git 等仍需人工批准）、claude 新式 ``pattern:*`` 后缀语义。
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _settings_enabled() -> bool:
    """项目级 settings.json 默认关；显式 EMBERPY_PROJECT_SETTINGS=1 才读。"""
    return os.environ.get("EMBERPY_PROJECT_SETTINGS") == "1"


def load_settings_rules(workspace: Path) -> tuple[list[str], list[str]]:
    """返回 (deny_rules, allow_rules) —— settings 里 permissions 数组的原始字符串。

    只在门控开启时读 ``<workspace>/.claude/settings.json`` 的 ``permissions`` 块；
    这里只做"忠实取回数组"，文法校验/坏条目丢弃交给 PermissionPolicy 构造。
    文件缺失 / 坏 JSON / 形状不对都返回空列表，不抛。
    """
    deny: list[str] = []
    allow: list[str] = []
    if not _settings_enabled():
        return deny, allow
    cfg = Path(workspace).resolve() / ".claude" / "settings.json"
    try:
        obj = json.loads(cfg.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return deny, allow
    if not isinstance(obj, dict):
        return deny, allow
    perms = obj.get("permissions")
    if not isinstance(perms, dict):
        return deny, allow
    for key, store in (("deny", deny), ("allow", allow)):
        val = perms.get(key)
        if isinstance(val, list):
            store.extend(str(x) for x in val)
    return deny, allow
