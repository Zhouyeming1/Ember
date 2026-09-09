"""权限门：按当前模式裁决 写文件 / shell 命令 / 读文件 三类操作。

confirm 是可选的交互回调；需要询问但没给 confirm 时一律拒绝（fail closed），
这样在测试与无头场景下最安全。

工具级 allow/deny（来自 .claude/settings.json，见 policy.py/settings.py）：
deny 无条件压过任何模式与 confirm；allow 只豁免各 authorize_* 里的"普通询问"，
敏感集合（.env/.git…）与 deny 均不可被 allow 绕过。工具名取自 request_context
（Agent 每次工具调用前写入 tool_name/tool_input，见 agent/core.py）。

策略（policy）可选注入：deny 规则无条件 + 敏感文件/目录集合内置。不传时 gate
内部给默认空策略（deny 空、敏感集合仍生效）——.git/.bashrc 这类在 full/auto 下
不能静默写的语义恒成立（对齐 claude DANGEROUS 文件/目录）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from ..errors import Denied
from .modes import CommandKind, PermissionMode
from .policy import PermissionPolicy
from .rules import classify_command
from .scope import is_within

Confirm = Callable[[str], bool]
# PermissionRequest 裁决器：读 {tool_name, tool_input, mode, prompt} 上下文，
# 返回 "allow"/"deny"/None（None=继续走人工/默认路径）。由 Agent 注入（经 hooks）。
PermissionDecider = Callable[[dict[str, Any]], Optional[str]]


class PermissionGate:
    def __init__(
        self,
        mode: PermissionMode,
        workspace: Path,
        policy: Optional[PermissionPolicy] = None,
    ) -> None:
        self.mode = mode
        self.workspace = workspace
        self.policy = policy or PermissionPolicy(workspace)
        # PermissionRequest：询问人之前先问 hook 能否代答（allow/deny/ask）。
        # decider 由 Agent 注入（内部触发 PermissionRequest hook）；request_context 由
        # Agent 在每次工具调用前写入当前工具名+入参，让 hook 知道"这次在问什么"。
        self.decider: Optional[PermissionDecider] = None
        self.request_context: dict[str, Any] = {}

    # -- 写文件 ----------------------------------------------------------
    def authorize_write(
        self,
        path: Path,
        confirm: Optional[Confirm] = None,
        *,
        lexical: Optional[Path] = None,
    ) -> None:
        """裁决一次文件写入。path 是 resolve() 后的 canonical 路径；lexical 可选，
        是用户给的原始词法路径（不展开 symlink）——deny/敏感判定对两者都查，
        覆盖"软链指向工作区内 .git"等 canonical 看不到而用户词法路径看得到的场景。
        """
        policy = self.policy
        # deny 无条件：压过 confirm / 任何模式（对齐 claude "deny 免疫一切"）
        for cand in (path, lexical):
            if cand is not None and policy.denies(cand):
                raise Denied(f"写文件 {cand}", "命中 deny 规则（无条件拒绝）")
        # 工具级 deny（settings.json）：同样无条件，压过 full/confirm
        if self._tool_denied(policy.path_spec(path)):
            raise Denied(f"写文件 {path}", "命中 deny 规则（无条件拒绝）")
        if self.mode is PermissionMode.PLAN:
            raise Denied("写文件", "plan 模式只读")
        # 敏感文件/目录：full/auto 下也不能静默写——需要一次人工批准（无交互即拒）
        for cand in (path, lexical):
            if cand is None:
                continue
            reason = policy.sensitive_reason(cand)
            if reason is not None:
                label = f"写文件 {cand}"
                if self._ask(confirm, f"{label}（{reason}）是否允许？"):
                    return
                raise Denied(label, "敏感文件需人工批准（无交互即拒绝）")
        within = is_within(path, self.workspace)
        if self.mode is PermissionMode.FULL:
            return
        if within and self.mode is PermissionMode.AUTO:
            return
        # ASK 模式，或 AUTO 模式下写到工作区外 -> 询问
        # allow（settings.json）只豁免"普通询问"：前面的敏感分支仍需人工批准，deny 免疫一切
        if self._tool_allowed(policy.path_spec(path)):
            return
        label = f"写文件 {path}"
        if self._ask(confirm, f"{label}（权限模式 {self.mode.value}）是否允许？"):
            return
        raise Denied(label, "未获用户批准")

    # -- shell 命令 ------------------------------------------------------
    def authorize_command(self, command: str, confirm: Optional[Confirm] = None) -> None:
        kind = classify_command(command, self.workspace)
        if kind is CommandKind.BLOCKED:
            raise Denied(f"执行命令：{command}", "命中硬拦截（无论任何权限模式都不允许）")
        # 工具级 deny（settings.json）：无条件，压过 full/confirm
        if self._tool_denied(command):
            raise Denied(f"执行命令：{command}", "命中 deny 规则（无条件拒绝）")

        # full：除了硬拦截，一律放行
        if self.mode is PermissionMode.FULL:
            return

        if self.mode is PermissionMode.PLAN:
            if kind is CommandKind.READ_ONLY:
                return
            raise Denied(f"执行命令：{command}", "plan 模式只允许只读诊断命令")

        # ASK：除了只读诊断命令，都询问
        if self.mode is PermissionMode.ASK:
            if kind is CommandKind.READ_ONLY:
                return
            if self._tool_allowed(command):
                return
            if self._ask(confirm, f"执行命令：{command} 是否允许？"):
                return
            raise Denied(f"执行命令：{command}", "未获用户批准")

        # AUTO：常规放行，危险/越界询问
        if self.mode is PermissionMode.AUTO:
            if kind in (CommandKind.READ_ONLY, CommandKind.SAFE):
                return
            if self._tool_allowed(command):
                return
            if self._ask(confirm, f"执行（较危险）命令：{command} 是否允许？"):
                return
            raise Denied(f"执行命令：{command}", "未获用户批准")

        raise Denied(f"执行命令：{command}", f"未知权限模式 {self.mode}")

    # -- 读文件 ----------------------------------------------------------
    def authorize_read(self, path: Path, confirm: Optional[Confirm] = None) -> None:
        # deny 无条件：读 deny 名单里的文件同样拒绝（对齐 claude "deny 免疫一切"）
        if self.policy.denies(path):
            raise Denied(f"读文件 {path}", "命中 deny 规则（无条件拒绝）")
        # 工具级 deny（settings.json）：Read(...) 规则能拦读 .env 等（读只受 deny 约束，
        # 不受敏感集合约束——所以工具 deny 是唯一能按文件拦读的配置口）
        if self._tool_denied(self.policy.path_spec(path)):
            raise Denied(f"读文件 {path}", "命中 deny 规则（无条件拒绝）")
        within = is_within(path, self.workspace)
        if within:
            return
        if self.mode is PermissionMode.FULL:
            return
        if self.mode is PermissionMode.PLAN:
            raise Denied(f"读文件 {path}", "plan 模式只能读工作区内文件")
        if self._ask(confirm, f"读工作区外文件 {path} 是否允许？"):
            return
        raise Denied(f"读文件 {path}", "未获用户批准")

    # -- 外部工具（MCP）--------------------------------------------------
    def authorize_external(self, tool_name: str, confirm: Optional[Confirm] = None) -> None:
        """外部进程工具（MCP server 提供的工具）调用前过这一道。

        语义：FULL 放行；PLAN 拒绝（外部副作用无法保证只读，plan 是只读调研期）；
        AUTO 放行（显式配置了该 server = 信任它干自己的活，与内置写工具在 AUTO
        工作区内放行同一信任级）；ASK 询问。无 confirm 时一律拒绝（fail closed）。
        引擎无法预知 server 会做什么，信任根在"用户显式把该 server 配进配置"。
        """
        # 工具级 deny（settings.json）：无条件，压过 full/auto（spec=tool_name 本身）
        if self._tool_denied(tool_name):
            raise Denied(f"外部工具 {tool_name}", "命中 deny 规则（无条件拒绝）")
        if self.mode in (PermissionMode.FULL, PermissionMode.AUTO):
            return
        if self.mode is PermissionMode.PLAN:
            raise Denied(f"外部工具 {tool_name}", "plan 模式不调用外部工具")
        # ASK（及未知模式兜底）：询问；allow 只豁免普通询问
        if self._tool_allowed(tool_name):
            return
        if self._ask(confirm, f"调用外部工具 {tool_name} 是否允许？"):
            return
        raise Denied(f"外部工具 {tool_name}", "未获用户批准")

    # -- 内部 ------------------------------------------------------------
    def _tool_name(self) -> Optional[str]:
        """当前正在执行的工具名（Agent 在每次工具调用前写入 request_context）。"""
        ctx = self.request_context
        if isinstance(ctx, dict):
            return ctx.get("tool_name")
        return None

    def _tool_denied(self, spec: Optional[str]) -> bool:
        """工具级 deny（settings 规则）：无条件，gate 在任何模式之前调。"""
        name = self._tool_name()
        return bool(name) and self.policy.denies_tool(name, spec)

    def _tool_allowed(self, spec: Optional[str]) -> bool:
        """工具级 allow（settings 规则）：只豁免"普通询问"，见各 authorize_* 调用点。"""
        name = self._tool_name()
        return bool(name) and self.policy.allows_tool(name, spec)

    def _request_decision(self, prompt: str) -> Optional[str]:
        """PermissionRequest 裁决：hook 返回 allow/deny/ask（None=交给人工确认）。"""
        if self.decider is None:
            return None
        ctx: dict[str, Any] = dict(self.request_context)
        ctx.setdefault("mode", self.mode.value)
        ctx["prompt"] = prompt
        try:
            return self.decider(ctx)
        except Exception:
            return None  # hook 异常不能把权限请求搞挂：退回默认询问

    def _ask(self, confirm: Optional[Confirm], prompt: str) -> bool:
        # PermissionRequest hook 先裁决：allow=免问放行；deny=直接拒绝（不落人工）
        decision = self._request_decision(prompt)
        if decision == "allow":
            return True
        if decision == "deny":
            raise Denied(f"权限请求：{prompt}", "PermissionRequest hook 已代答拒绝（deny）")
        if confirm is None:
            return False  # 无交互能力时拒绝，宁严勿松
        try:
            return bool(confirm(prompt))
        except Exception:
            return False
