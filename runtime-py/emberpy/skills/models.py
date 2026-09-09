"""技能模型：一条 Skill = 元数据 + 正文 + 来源信息。

与 claude 的 ``Command(prompt)`` 对齐但砍掉命令面用不到的大多数字段，
只保留驱动本引擎行为需要的：名字/描述/when_to_use、allowed-tools（目前
仅元数据）、参数名（用于 $ARGUMENTS 之外具名插值）、user-invocable /
disable-model-invocation（决定出现在 /skill: 命令面还是只给模型 Skill 工具）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Skill:
    name: str                 # 逻辑名（命名空间用 : 分隔，如 a:b）
    description: str          # 一句话描述（可空，回退正文首行）
    body: str                 # 去掉 frontmatter 后的技能正文
    path: Path                # 技能文件绝对路径
    base_dir: Optional[Path]  # SKILL.md 所在目录（模型据此解析相对引用）
    source: str               # 来源标签：project / user / override
    when_to_use: str = ""
    allowed_tools: tuple[str, ...] = ()
    argument_names: tuple[str, ...] = ()
    argument_hint: str = ""
    version: str = ""
    user_invocable: bool = True          # false => 不进 /skill: 命令面
    disable_model_invocation: bool = False  # true => 模型 Skill 工具拒绝调用
    fork: bool = False                   # context: fork => 在隔离子 agent 里执行（正文不进主上下文）
    paths: tuple[str, ...] = ()          # 条件技能（paths frontmatter）：命中的触碰路径才激活（见 store）

    @property
    def hidden(self) -> bool:
        return not self.user_invocable
