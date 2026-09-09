"""agentic 循环内核：把模型、工具、权限门、补丁与会话串成一次运行。

流程：
    用户任务
      -> 组装历史消息（含续跑裁剪）
      -> 调模型；模型返回 assistant 消息（可能带多个 tool_calls）
      -> 逐个执行工具：权限检查在工具内部完成，结果作为 tool 消息回填
      -> 再调模型……直到模型不再要工具，输出最终答复

要点：
- 执行工具的每一步都计数，超过 max_steps 强制停（防死循环）
- 工具异常一律转成文本回给模型（让模型自己修正），不让循环崩掉
- 权限拒绝也作为 tool 结果回给模型（让它明白为什么不行）
- 每步通过 on_event 广播，UI/CLI 据此实时展示

对应 claude-code-analysis 里 assistant / 编排那一层的角色。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from ..compact import split_old_events
from ..errors import Denied
from ..hooks import HookManager, HookRunOutcome
from ..llm import AssistantReply, LLM, Message, without_reasoning
from ..llm.usage import UsageTracker
from ..memory import MemoryStore, build_memory_prompt
from ..patches import PatchStore
from ..permission import PermissionGate, PermissionMode, PermissionPolicy
from ..session import Session, SessionStats
from ..tools import ToolEnv, ToolRegistry, default_registry
from ..tools.agent_tool import (
    AGENT_TYPE_EXPLORE,
    AGENT_TYPE_GENERAL,
    apply_agent_def_filters,
    build_agent_tool,
    build_subagent_registry,
)
from .inputs import RuntimeInput
from .prompts import SYSTEM_TEMPLATE, build_instructions, plan_mode_guide

EventCallback = Callable[[str, dict[str, Any]], None]

# PostToolUse 的 tool_response 截断上限（防把超长工具输出整个喂给 hook）
_HOOK_TOOL_RESPONSE_MAX = 20_000


def _clip_output(output: Any) -> str:
    """把工具返回值截成适合塞给 PostToolUse hook 的字符串。"""
    text = output if isinstance(output, str) else str(output)
    if len(text) <= _HOOK_TOOL_RESPONSE_MAX:
        return text
    return text[:_HOOK_TOOL_RESPONSE_MAX] + "\n…(工具输出过长，已截断)"


@dataclass
class RunResult:
    final_content: Optional[str]
    steps: int
    stats: SessionStats
    stopped_by_limit: bool = False  # 因达到最大步数而提前停下
    interrupted: bool = False       # 收到外部停止请求（如 UI 的 stop 按钮）而提前停下


class Agent:
    def __init__(
        self,
        *,
        llm: LLM,
        workspace: Path,
        mode: PermissionMode,
        registry: Optional[ToolRegistry] = None,
        env: Optional[ToolEnv] = None,
        session: Optional[Session] = None,
        max_steps: int = 40,
        on_event: Optional[EventCallback] = None,
        runtime_input: Optional[RuntimeInput] = None,
        memory: Optional[MemoryStore] = None,
        allow_subagents: bool = False,
        subagent_llm: Optional[LLM] = None,
        hooks: Optional[HookManager] = None,
        policy: Optional[PermissionPolicy] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        # 自定义 agent 定义文件的正文当 system prompt（R-E）：给定时 _system_prompt
        # 原样用它，不走默认模板（只在子 agent 用；主 agent 不传）。
        self.system_prompt = system_prompt
        self.llm = llm
        self.workspace = workspace.resolve()
        self.mode = mode if isinstance(mode, PermissionMode) else PermissionMode(mode)
        # policy（deny 规则 + 敏感集合）由 worker 装配一次；不给时 gate 内部给默认空
        # policy（敏感集合仍常开）。deny 不能被 agent/子 agent 绕开：同一对象跨层共享。
        self.gate = PermissionGate(self.mode, self.workspace, policy=policy)
        self.patches = PatchStore()
        # env 可用来从外部传入 confirm（交互回调）等；gate/patches 缺了就用自己的
        self.env = env or ToolEnv(workspace=self.workspace, gate=self.gate, patches=self.patches)
        if self.env.gate is None:
            self.env.gate = self.gate
        if self.env.patches is None:
            self.env.patches = self.patches
        # 显式给了记忆库（或 env 里已挂上）就启用：工具集 + system prompt 都看 env.memory
        if self.env.memory is None and memory is not None:
            self.env.memory = memory
        self.allow_subagents = allow_subagents
        # 子 agent 的模型：默认与父同一个（测试可注入独立 FakeLLM，方便给子 agent 单独写脚本）
        self.subagent_llm = subagent_llm
        # allow_subagents 时先把 spawner 挂上 env 再建默认注册表：fork 技能要经它
        # 隔离执行（正文不进主上下文）。顺序不能反——default_registry 建的 Skill 工具
        # fn 每次调用都读 env.spawner，此时读到的是本 agent 的派生回调。
        if allow_subagents:
            self.env.spawner = self._spawn_subagent
        self.registry = registry or default_registry(self.env)
        # 允许派生时给注册表追加 run_agent（子 agent 的注册表由 _spawn_subagent 重建，
        # 不含本工具 -> 结构性保证"子 agent 不能再派生子 agent"，对齐 claude）。
        if allow_subagents:
            # run_agent 描述动态列出自定义 agent 定义（env.agents，R-E）；description
            # 是 callable，schema 每次重算时清单最新，无需重建注册表。
            self.registry = ToolRegistry(
                [
                    *self.registry.all(),
                    build_agent_tool(self._spawn_subagent, getattr(self.env, "agents", None)),
                ]
            )
        self.session = session or Session(cwd=self.workspace)
        self.max_steps = max_steps
        self.on_event = on_event
        self.runtime_input = runtime_input
        self._logged_patch_seq = 0  # 已同步进 session 的补丁 seq 水位
        # 会话级 usage/cost 累加：每次模型答复后 track，供 worker 填 stats 面板
        self.usage = UsageTracker()
        # 上下文超限自适应恢复的状态：记录最近一次模型调用错误，以及是否已裁旧段重试过
        # （每个 run 只兜底一次；正常路径靠 auto-compact 在 prompt 前预防）
        self._last_llm_error: Optional[Exception] = None
        self._context_recovered = False
        # hooks：外部命令钩子（PreToolUse/PostToolUse/SubagentStart/Stop…）。由宿主
        # 构造一次传入；不传（测试/子 agent/纯库）=> 关闭，全部触发点 no-op。
        self.hooks = hooks
        # PermissionRequest：把 gate 的"询问人"前插一道 hook 裁决（allow/deny/ask）。
        # 工具共用 env.gate；装了 hooks 就挂 decider（没配 PermissionRequest 事件时
        # decider 内部直接返回 None，仍是原人工/默认路径）。
        if self.hooks is not None:
            gate = self.env.gate
            if gate is not None and getattr(gate, "decider", None) is None:
                gate.decider = self._permission_decision

    def set_mode(self, mode: PermissionMode | str) -> None:
        """运行中切换权限模式（worker 收到 /permissions 时调用）。

        只影响之后的行为：权限门模式实时切换，下一轮 system prompt 也随之更新。
        """
        self.mode = mode if isinstance(mode, PermissionMode) else PermissionMode(mode)
        self.gate.mode = self.mode

    # -- 对外 ------------------------------------------------------------
    def run(self, task: str) -> RunResult:
        self._emit("start", {"task": task, "mode": self.mode.value})
        # 动态发现：同一会话续跑之间磁盘上可能新增/删除技能，每轮开始重扫一次根。
        # Skill 工具/命令面对象引用的是同一个 store，refresh 原地更新即可被感知。
        skills = self.env.skills
        if skills is not None:
            try:
                refresh = getattr(skills, "refresh", None)
                if callable(refresh):
                    refresh()
            except Exception:
                pass  # 技能库刷新失败不阻断任务（个别坏文件在 load 内部已被容忍）
        # 自定义 agent 定义同技能一样动态发现：同一会话续跑之间新增/删掉的
        # .claude/agents/*.md 下一轮就出现在 run_agent 的可选清单里（R-E）。
        agents_store = getattr(self.env, "agents", None)
        if agents_store is not None:
            try:
                refresh = getattr(agents_store, "refresh", None)
                if callable(refresh):
                    refresh()
            except Exception:
                pass

        messages: list[Message] = []
        # system prompt 每轮都带：计划模式下额外拼计划模式指引（只读调研 -> update_plan -> 停下）。
        # 记忆区块带当前任务文本——索引条目多时按相关度挑全文（见 memory.build_memory_prompt）。
        messages.append(self._system_prompt(task))

        history = self.session.resume_messages()
        # 已是最新一条相同 user 消息时不重复追加（防重入）
        if not (history and history[-1].get("role") == "user" and history[-1].get("content") == task):
            self.session.add_user(task)
            history = self.session.resume_messages()
        messages.extend(history)

        steps = 0
        final_content: Optional[str] = None
        interrupted = False
        ended_by_model_error = False

        while steps < self.max_steps:
            if self._stop_requested():
                interrupted = True
                break
            self._drain_steers(messages)
            # schemas 每步重算（R-F）：Skill 工具描述随激活技能集合/agents 清单热更，
            # 前一步文件改动激活的路径条件技能，下一步模型就能看到。
            schemas = self.registry.schemas()
            reply = self._call_model(messages, schemas)
            if reply is None:
                # 上下文超限的兜底恢复：把"最后一个 user 之前"的旧事件裁掉重发一次。
                # 这是对预料之外超限的最后防线（正常路径由 auto-compact 在 prompt 前
                # 预防）；裁完仍失败（如单轮本身就超限）就正常报错，交给上层。
                err = self._last_llm_error
                if (
                    not self._context_recovered
                    and err is not None
                    and getattr(err, "context_exceeded", False)
                    and self._trim_events_for_context()
                ):
                    self._context_recovered = True
                    self._last_llm_error = None
                    messages = [self._system_prompt(task)] + self.session.resume_messages()
                    continue
                ended_by_model_error = True
                break  # 调用失败已广播，交给上层

            self.usage.track(reply.usage)  # 每次模型答复计入会话级 token/cost 统计

            raw_assistant = reply.raw_message or {
                "role": "assistant",
                "content": reply.content,
            }
            self.session.add_assistant(raw_assistant)
            # 发回模型的上下文要去掉推理文本（reasoner 规范）；展示/落盘保留原文
            messages.append(without_reasoning(raw_assistant))
            self._emit("assistant", raw_assistant)

            if not reply.tool_calls:
                final_content = reply.content
                break

            for call in reply.tool_calls:
                steps += 1
                self._emit("tool_call", {"id": call.id, "name": call.name, "arguments": call.arguments})
                result, denied = self._execute(call)
                self.session.add_tool_result(call.id, result)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
                self._sync_patches()  # 把工具产生的写文件 checkpoint 记进会话
                self._emit(
                    "tool_result",
                    {
                        "id": call.id,
                        "name": call.name,
                        "denied": denied,
                        "preview": result[:2000],
                        "result": result,  # 完整结果（供界面展示，可能较长）
                    },
                )

        stopped = steps >= self.max_steps or interrupted
        self._emit("end", {"steps": steps, "stopped_by_limit": stopped, "interrupted": interrupted})
        # Stop/StopFailure hook：本轮 run 收尾（非阻塞；无配置即 no-op）。模型调用出错
        # 结束（且没被超限恢复救回）走 StopFailure，否则走 Stop（R-G②）。
        if ended_by_model_error:
            error = str(self._last_llm_error) if self._last_llm_error is not None else "模型调用失败"
            self._fire_hook("StopFailure", {"stop_hook_active": True, "error": error})
        else:
            self._fire_hook("Stop", {"stop_hook_active": True, "steps": steps, "interrupted": interrupted})
        return RunResult(
            final_content=final_content,
            steps=steps,
            stats=self.session.stats(),
            stopped_by_limit=steps >= self.max_steps,
            interrupted=interrupted,
        )

    def _spawn_subagent(self, description: str, prompt: str, agent_type: str = AGENT_TYPE_GENERAL) -> str:
        """run_agent 工具回调：同步跑一个独立子 agent，返回它的最终答复文本。

        参考 claude AgentTool 同步路径：子 agent 用独立会话（只有 prompt 一条 user
        消息）、独立重建的工具池（无 run_agent，不能递归派生）、与父共享工作区与
        PatchStore（子 agent 的文件改动进同一个 /undo 账本）。explore 类型只给
        只读工具。agent_type 除内置 general/explore 外，还能是 env.agents 里发现的
        自定义 agent 名（R-E：正文当 system prompt、按 tools 过滤工具池）。子 agent
        的事件不外发（父 UI 只见这一条 run_agent 工具轨迹）。派生前后触发
        SubagentStart/Stop hook（无配置即 no-op）。
        """
        agent_type = agent_type or AGENT_TYPE_GENERAL
        agent_def = None
        if agent_type not in (AGENT_TYPE_GENERAL, AGENT_TYPE_EXPLORE):
            # 自定义 agent：env.agents 里查不到就算无效 agent_type
            agents_store = getattr(self.env, "agents", None)
            if agents_store is not None:
                try:
                    agent_def = agents_store.get(agent_type)
                except Exception:
                    agent_def = None
            if agent_def is None:
                allowed = f"{AGENT_TYPE_GENERAL}/{AGENT_TYPE_EXPLORE}"
                extra = []
                if agents_store is not None:
                    try:
                        extra = agents_store.names()
                    except Exception:
                        extra = []
                if extra:
                    allowed += "/" + "/".join(extra)
                return f"run_agent 参数 agent_type 只支持 {allowed}，收到 {agent_type!r}。"
        self._fire_hook(
            "SubagentStart",
            {
                "subagent_type": agent_type,
                "description": description or "",
                "custom": agent_def is not None,
            },
        )
        try:
            return self._spawn_subagent_inner(description, prompt, agent_type, agent_def)
        finally:
            self._fire_hook(
                "SubagentStop",
                {
                    "subagent_type": agent_type,
                    "description": description or "",
                    "custom": agent_def is not None,
                },
            )

    def _spawn_subagent_inner(
        self,
        description: str,
        prompt: str,
        agent_type: str,
        agent_def=None,
    ) -> str:
        """_spawn_subagent 的实际执行体（子 agent 的独立上下文；见外层 docstring）。

        agent_def 非 None（自定义 agent）时：正文当 system prompt、initial_prompt 前置、
        max_turns 覆盖步数上限、permissionMode 覆盖权限模式、tools/disallowedTools
        过滤子工具池。内置 general/explore 走原共享 gate 路径（行为不变）。
        """
        try:
            child_mode = self.mode
            child_gate = self.env.gate
            child_system = None
            child_max_steps = None
            task = prompt
            base_type = agent_type
            if agent_def is not None:
                # 自定义 agent：权限模式/步数/system prompt 都由定义覆盖；deny 与敏感
                # 集合通过共享同一 policy 对象仍成立（gate 换新实例但不丢规则）。
                if agent_def.permission_mode:
                    try:
                        child_mode = PermissionMode(agent_def.permission_mode)
                    except ValueError:
                        child_mode = self.mode
                child_gate = PermissionGate(
                    child_mode,
                    self.workspace,
                    policy=getattr(self.env.gate, "policy", None),
                )
                child_system = agent_def.body
                if agent_def.max_turns and agent_def.max_turns > 0:
                    child_max_steps = agent_def.max_turns
                if agent_def.initial_prompt:
                    task = agent_def.initial_prompt + "\n\n" + prompt
                base_type = AGENT_TYPE_GENERAL  # 自定义不走 explore 只读过滤

            # 子 agent 不继承 ask/memory/skills：不打断用户、不污染记忆库、不引技能；
            # 权限门保留 deny 语义（deny 不能被子 agent 绕过）。
            child_env = ToolEnv(
                workspace=self.workspace,
                gate=child_gate,
                patches=self.env.patches,  # 共享：子 agent 的写文件进同一个 /undo 账本
                confirm=self.env.confirm,
            )
            child_registry = build_subagent_registry(default_registry(child_env), base_type)
            if agent_def is not None and agent_def.filters_tools:
                child_registry = apply_agent_def_filters(child_registry, agent_def)
            child = Agent(
                llm=self.subagent_llm if self.subagent_llm is not None else self.llm,
                workspace=self.workspace,
                mode=child_mode,
                env=child_env,
                registry=child_registry,
                session=Session(cwd=self.workspace),
                on_event=None,  # 子 agent 不广播事件：父只发 run_agent 一条工具轨迹
                runtime_input=None,
                allow_subagents=False,
                max_steps=child_max_steps if child_max_steps is not None else self.max_steps,
                system_prompt=child_system,
            )
            result = child.run(task)
            self.usage.merge(child.usage)  # 子 agent 的 token/cost 并入父会话统计
        except Exception as exc:
            return f"派生子 agent 出错：{exc}"
        text = (result.final_content or "").strip()
        if text:
            return text
        if result.interrupted:
            return f"子 agent 被中断，未产出最终答复（已执行 {result.steps} 步）。"
        return (
            f"子 agent 未产出文本答复（已执行 {result.steps} 步"
            f"{'，因达到步数上限提前停下' if result.stopped_by_limit else ''}）。"
            "它可能只完成了工具调用。请检查文件状态或让用户补充信息。"
        )

    def undo_last_change(self) -> Optional[str]:
        """回滚最近一次 write_file，返回说明文本（/undo 用）。"""
        patch = self.patches.undo_last()
        if patch is None:
            return "没有可回滚的改动。"
        # undo 之后把水位校准，避免下次写文件时漏记或重复记账
        self._logged_patch_seq = max(self._logged_patch_seq, patch.seq)
        return f"已回滚：{patch.summary}"

    def _sync_patches(self) -> None:
        """把新产生的写文件补丁追加为会话的 patch 事件（用于统计/后续恢复）。"""
        for patch in self.patches.since_seq(self._logged_patch_seq):
            self.session.add_patch(patch)
            self._logged_patch_seq = patch.seq

    # -- 外部输入（RPC 桥接） --------------------------------------------
    def _stop_requested(self) -> bool:
        rt = self.runtime_input
        return bool(rt and rt.stop_requested and rt.stop_requested())

    def _drain_steers(self, messages: list[Message]) -> None:
        """把积压的 steer 消息作为 user 消息插入（下一轮模型调用前）。"""
        rt = self.runtime_input
        if not (rt and rt.drain_steers):
            return
        for text in rt.drain_steers() or []:
            if not text or not text.strip():
                continue
            self.session.add_user(text)
            messages.append({"role": "user", "content": text})
            self._emit("user_injected", {"text": text})
            if rt.on_user_injected:
                try:
                    rt.on_user_injected(text)
                except Exception:
                    pass

    # -- 内部 ------------------------------------------------------------
    def _system_prompt(self, task: str = "") -> Message:
        # 自定义 agent 定义正文当整段 system prompt（R-E）：不拼默认模板/CLAUDE.md/记忆
        if self.system_prompt is not None:
            return {"role": "system", "content": self.system_prompt}
        content = SYSTEM_TEMPLATE.format(workspace=self.workspace, mode=self.mode.value)
        if self.mode is PermissionMode.PLAN:
            # 计划模式附加指引：只读调研 -> update_plan 列计划 -> 停下等批准
            content += "\n\n" + plan_mode_guide()
        # 项目指令（CLAUDE.md/AGENTS.md）：SYSTEM_TEMPLATE 之后、记忆之前注入。
        # 无文件返回空串 -> 不加块（对齐 claude "没有 CLAUDE.md 就省略 key"）。
        instr = build_instructions(self.workspace)
        if instr:
            content += "\n\n" + instr
        mem = self.env.memory
        if mem is not None:
            # 记忆区块整段拼在系统提示里：索引随每次 run 刷新，跨会话自动带上前几轮的记忆；
            # 记忆多且 task 非空时，task 用于按相关度挑少量记忆全文（两级召回）
            content += "\n\n" + build_memory_prompt(mem, task)
        if self.allow_subagents:
            content += (
                "\n\n需要把一段独立子任务委托出去时用 run_agent（general 全功能；"
                "explore 只读调研，适合让子 agent 去搜索/读代码）。子 agent 看不到"
                "本对话历史，prompt 里要给全背景；它的最终答复会作为工具结果返回，"
                "请在给用户的答复里概括它做了什么、结果如何。"
            )
        return {"role": "system", "content": content}

    def _call_model(self, messages: list[Message], schemas: list[dict[str, Any]]) -> Optional[AssistantReply]:
        self._emit("model_request", {"messages": len(messages)})
        try:
            # LLM 协议只保证 complete；实现了 stream_complete 的模型走真流式，
            # 增量通过 on_delta 转发成 assistant_delta 事件（UI 据此发 message_update）。
            # FakeLLM 等测试替身没有 stream_complete，自动落回整包返回，现有用例零改动。
            stream = getattr(self.llm, "stream_complete", None)
            if callable(stream):
                return stream(messages, tools=schemas, on_delta=self._on_stream_delta)
            return self.llm.complete(messages, tools=schemas)
        except Exception as exc:  # 网络/鉴权等上层错误：广播后让调用方决定
            self._last_llm_error = exc
            self._emit("model_error", {"error": str(exc)})
            return None

    def _trim_events_for_context(self) -> bool:
        """裁掉旧段事件（保留最后一个 user 消息起的整轮），为超限重发腾上下文。

        复用 compact 的切段语义（split_old_events），不做 LLM 摘要——省一次模型
        调用，且不把"摘要机制"接进热路径。没有可裁的旧段返回 False。
        """
        old, keep = split_old_events(self.session.events())
        if not old:
            return False
        self.session.replace_events(keep)
        return True

    def _on_stream_delta(self, text: str, thinking: Optional[str]) -> None:
        """流式增量回调：把累计 text/thinking 转成 assistant_delta 事件（worker 发 message_update）。"""
        self._emit("assistant_delta", {"text": text, "thinking": thinking})

    def _execute(self, call) -> tuple[str, bool]:
        tool = self.registry.get(call.name)
        if call.parse_error:
            return call.parse_error, False
        if tool is None:
            available = ", ".join(self.registry.names())
            return f"未知工具 {call.name}。可用工具：{available}", False
        # PreToolUse hook：matcher 匹配工具名；阻塞则拒绝执行（不给 fn 机会）
        blocked = self._pre_tool_blocked(call)
        if blocked is not None:
            return blocked, True
        hook_args = {
            "tool_name": call.name,
            "tool_input": call.arguments,
            "tool_use_id": call.id,
        }
        # 记录"这次权限在问哪个工具"（PermissionRequest hook 的 payload 上下文）。
        # 工具在 fn 内部过权限门，decider 要能知道当前工具名/入参。
        g = self.env.gate
        if g is not None and hasattr(g, "request_context"):
            g.request_context = {"tool_name": call.name, "tool_input": call.arguments}
        try:
            output = tool.fn(**call.arguments)
            # PostToolUse：成功侧触发（tool_response 截断，防巨输出撑爆 hook）
            self._fire_hook(
                "PostToolUse",
                {**hook_args, "tool_response": _clip_output(output)},
                matcher_target=call.name,
            )
            return (output if isinstance(output, str) else str(output)), False
        except Denied as exc:
            # R-G③：权限拒绝统一在这层处理——工具不再把 Denied 吞成普通文本（denied
            # 标志之前恒 False，UI/转录分不出"拒绝"与"普通错误"）。这里触发
            # PermissionDenied hook、denied=True（worker 据此标 isError），文本仍回给
            # 模型让它知道为什么被拦并调整。
            self._fire_hook(
                "PermissionDenied",
                {**hook_args, "reason": exc.reason},
                matcher_target=call.name,
            )
            return str(exc), True
        except TypeError as exc:
            # 缺参/参数名不对：提示模型检查 schema，比裸异常友好
            self._fire_hook(
                "PostToolUseFailure",
                {**hook_args, "tool_error": str(exc)},
                matcher_target=call.name,
            )
            return f"调用 {call.name} 参数有误：{exc}。请对照该工具的参数说明重试。", False
        except Exception as exc:
            self._fire_hook(
                "PostToolUseFailure",
                {**hook_args, "tool_error": str(exc)},
                matcher_target=call.name,
            )
            return f"执行 {call.name} 出错：{exc}", False
        finally:
            if g is not None and hasattr(g, "request_context"):
                g.request_context = {}

    def _pre_tool_blocked(self, call) -> Optional[str]:
        """PreToolUse 决策：某规则阻塞 => 返回拒绝理由文本（含工具名）；否则 None。"""
        if self.hooks is None or not self.hooks.has("PreToolUse"):
            return None
        outcome = self._fire_hook(
            "PreToolUse",
            {
                "tool_name": call.name,
                "tool_input": call.arguments,
                "tool_use_id": call.id,
            },
            matcher_target=call.name,
        )
        reason = outcome.blocked_reason
        if reason is None:
            return None
        return f"权限拒绝：hook PreToolUse 阻止了工具 {call.name}（{reason}）"

    def _fire_hook(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        matcher_target: Optional[str] = None,
    ) -> HookRunOutcome:
        """触发一个事件的所有匹配 hook（无配置/无匹配 = 纯 no-op）。

        注入上下文 env（事件/CWD/权限模式/会话转录路径），把 hook 的可见输出转发
        成 ``hook`` 事件（供 UI/测试观察），返回聚合结果给调用方做决策。
        """
        manager = self.hooks
        if manager is None or not manager.has(event):
            return HookRunOutcome(event=event)
        extra_env: dict[str, str] = {
            "HOOK_EVENT": event,
            "CWD": str(self.workspace),
            "PERMISSION_MODE": self.mode.value,
        }
        session_path = getattr(self.session, "path", None)
        if session_path is not None:
            extra_env["TRANSCRIPT_PATH"] = str(session_path)
        outcome = manager.run(
            event,
            payload,
            matcher_target=matcher_target,
            cwd=str(self.workspace),
            extra_env=extra_env,
        )
        # 广播可见文本 + 每条执行注记（若宿主不监听该事件则无害）
        for text in [*outcome.texts, *outcome.notes]:
            self._emit("hook", {"event": event, "text": text})
        return outcome

    def _permission_decision(self, ctx: dict[str, Any]) -> Optional[str]:
        """gate 的 PermissionRequest 裁决器：触发 hook，按 allow/deny/ask 代答。

        没配 PermissionRequest（或无匹配 rule / hook 没给决策）返回 None -> gate
        退回人工/默认路径（R-G④）。由 gate.decider 在每次"询问人"前调用。
        """
        if self.hooks is None or not self.hooks.has("PermissionRequest"):
            return None
        try:
            outcome = self._fire_hook("PermissionRequest", ctx, matcher_target=ctx.get("tool_name"))
            decision = outcome.decision
            return decision if decision in ("allow", "deny", "ask") else None
        except Exception:
            return None  # hook 异常不能让权限请求崩掉

    def _emit(self, type_: str, data: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(type_, data)
            except Exception:
                pass  # 观察者不应影响运行


def run_once(
    *,
    task: str,
    llm: LLM,
    workspace: Path,
    mode: PermissionMode | str,
    session_path: Optional[Path] = None,
    on_event: Optional[EventCallback] = None,
    max_steps: int = 40,
) -> RunResult:
    """便捷入口：造一个 Agent 并跑一轮。"""
    session = Session(path=session_path, cwd=workspace) if session_path else Session(cwd=workspace)
    agent = Agent(llm=llm, workspace=workspace, mode=mode, session=session, on_event=on_event, max_steps=max_steps)
    try:
        return agent.run(task)
    finally:
        session.close()
