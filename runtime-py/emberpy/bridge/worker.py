"""emberpy.bridge.worker —— 给 Ember 桌面端当后端的 JSON-RPC worker。

Electron 主进程（src/main/agent-host.ts）用"换行分隔 JSON"在 stdio 上与
本进程通信，协议很小：

- 命令入（每行一个）：``{"type": <method>, "id": <字符串>, ...参数}``
- 命令出：``{"type": "response", "id": <同一 id>, "success": true, "data": ...}``
  失败时 ``success: false`` + ``error``
- 事件出：其它任何带 ``"type"`` 的行都会被主进程原样转发给界面

关键原则：**界面（src/renderer）是 spec。**
renderer 只消费一小撮事件、字段有限，本模块就按它需要的形状发，转录
（get_messages / get_entries）也存成它能 parse 的形状（见 transcript.py）。

agent 循环跑在后台线程，主线程继续读 stdin——因此运行中仍能收到并响应
abort（停止）/ steer（插话）/ extension_ui_response（权限确认框的回执，见 ui.py）。

一轮对话的事件时序：
    message_start(user)           回显用户消息（界面用它替换输入框的乐观气泡）
    message_start(assistant,文本)  助手文本；界面见末尾是助手就合并、是用户就另起
    tool_execution_start / end     每个工具一次：toolName / toolCallId / args / result
    ...（可能多轮「助手文本+工具」交替）
    agent_settled                  收尾：running 置 false、未结束的工具标 error

注意：助手 message 文本必须是**本段累计全文**——renderer 的 mergeAssistant 是
整文本覆盖（incoming.text || old.text），不是增量追加。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from .. import compact as _compact
from ..agent import Agent, RuntimeInput
from ..hooks import HookManager, HookRunOutcome, load_hook_manager
from ..llm import ChatLLM, LLM
from ..mcp import McpManager, manager_from_env
from ..memory import MemoryStore, resolve_memory_dir
from ..memory import extract as _extract  # 回合末记忆自动抽取（#10）
from ..permission import PermissionMode, PermissionPolicy, load_permission_policy
from ..agents import AgentStore, resolve_agent_roots
from ..skills import SkillStore, resolve_skill_roots
from ..permission.modes import effective_mode
from ..session import Session, allocate_session_path
from ..tools import ToolEnv
from .transcript import Transcript, now_ms, pi_message, text_part, thinking_part, tool_part
from .ui import UiConfirm

# ---------------------------------------------------------------------------
# 输出（线程安全）
# ---------------------------------------------------------------------------


class _Out:
    """把对象写成一行 UTF-8 JSON 输出。默认写 stdout，测试可换成 list。"""

    def __init__(self, write: Optional[Callable[[str], None]] = None) -> None:
        self._lock = threading.Lock()
        if write is None:
            def _stdout(text: str) -> None:
                sys.stdout.write(text)
                sys.stdout.flush()

            self._write = _stdout
        else:
            self._write = write

    def emit(self, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with self._lock:
            self._write(line + "\n")


# ---------------------------------------------------------------------------
# 动态模型代理：让 set_model 对下一次请求生效（每次 complete 现建 ChatLLM）
# ---------------------------------------------------------------------------


class _DynamicLLM:
    """延迟到"下一次请求"才现建 ChatLLM 的代理（让 set_model 对下轮生效）。

    每次 complete / stream_complete 都用最新工厂造一个真 ChatLLM，保证模型、
    key/base_url 都是当下值。测试注入的假模型不走本类（直接传给 Agent）。
    """

    def __init__(self, factory: Callable[[], LLM]) -> None:
        self._factory = factory

    def complete(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None):
        return self._factory().complete(messages, tools=tools)

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        on_delta: Optional[Callable[[str, Optional[str]], None]] = None,
    ):
        return self._factory().stream_complete(messages, tools=tools, on_delta=on_delta)

    @property
    def model_id(self) -> str:
        return self._factory().model_id


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

_ASYNC = object()  # 命令分派哨兵：prompt 等异步命令，响应稍后由运行线程发出

# 工具把权限拒绝/异常都转成文本返回；denied 标志恒为 False，这些前缀即"错误结果"
_IS_ERROR_PREFIXES = ("错误：", "权限拒绝：", "执行出错：")

# 用户直接输入 /skill:名称 [参数] 或历史里折叠过的 <skill name="…"> 块时，worker
# 把技能正文展开成任务喂给 agent（技能正文即工作流说明，模型照做）。行文见 _execute_prompt。
_SKILL_SLASH_RE = re.compile(r"^\s*/skill:([A-Za-z0-9._:-]+)(?:\s+([\s\S]+?))?\s*$")
_SKILL_BLOCK_RE = re.compile(r'^\s*<skill\s+name="([^"]+)"[^>]*>\s*([\s\S]*?)</skill>\s*$', re.IGNORECASE)

# 引擎级文本命令（Ember 渲染端通过 prompt 文本表达，worker 必须自己接住，不发给模型）：
# - /permissions <mode>：权限下拉切换，纯状态变更（不建 agent、不落盘会话）。
# - /plan execute      ：InspectPanel「批准计划」按钮 -> 退出计划模式并开始执行。
_PERMISSIONS_RE = re.compile(r"^\s*/permissions\s+(plan|ask|auto|full)\s*$", re.IGNORECASE)
_PLAN_EXECUTE_RE = re.compile(r"^\s*/plan\s+execute\s*$", re.IGNORECASE)

# 批准后喂给 agent 的执行任务：计划本身在对话上下文里（模型刚用 update_plan 列过）
PLAN_EXECUTE_TASK = (
    "计划已获用户批准。请现在按上面列出的计划开始执行：逐条完成每个步骤，"
    "每完成一步就调用 update_plan 把对应步骤标为 in_progress/completed（让用户看到实时进度），"
    "最后用一段简洁的总结答复用户（改了什么、验证结果如何）。"
)


def parse_permissions_command(text: str) -> str | None:
    """从 prompt 文本里解析 /permissions <mode>；不是该命令返回 None。"""
    m = _PERMISSIONS_RE.match(text)
    if not m:
        return None
    return m.group(1).lower()


def is_plan_execute_command(text: str) -> bool:
    return _PLAN_EXECUTE_RE.match(text) is not None


def _desktop_sessions_known() -> bool:
    """桌面端把会话目录 env 设给子进程；有它 = 桌面场景，新会话首个 prompt 自动落盘。

    测试 / 纯库用法（Worker(session_file=None) 且无这些 env）保持纯内存，
    避免污染真机 ~/.ember/sessions。
    """
    return bool(os.environ.get("EMBER_SESSIONS_DIR") or os.environ.get("EMBER_HOME"))


def _is_error_result(text: str) -> bool:
    return text.startswith(_IS_ERROR_PREFIXES)


def _permission_notice(mode: PermissionMode) -> str:
    """切换权限模式后回给用户的一句话（中文）。"""
    if mode is PermissionMode.PLAN:
        return (
            "已切换为 plan（计划模式：只读）。请描述任务，我会先只读调研并用 "
            "update_plan 列出实施步骤，等你批准后再执行。"
        )
    if mode is PermissionMode.FULL:
        return "已切换为 full（完全访问）。注意：本模式写文件/命令不询问、直接放行。"
    if mode is PermissionMode.ASK:
        return "已切换为 ask（写文件 / 执行命令前会逐个询问你）。"
    return "已切换为 auto（工作区内常规操作自动放行，危险操作会询问）。"


def _workspace_rel(workspace: Path, path: Any) -> str:
    """把文件路径换算成工作区相对路径（供展示 /undo 一致用相对路径）。

    写到了工作区外（仅 full 模式可能出现）退回文件名。
    """
    if not path:
        return ""
    try:
        return Path(str(path)).resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return Path(str(path)).name


class Worker:
    def __init__(
        self,
        *,
        workspace: Path,
        permission: str = "auto",
        sandbox: str = "workspace-write",
        provider: str = "deepseek",
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        session_file: Optional[str] = None,
        api_key: Optional[str] = None,
        llm: Optional[LLM] = None,  # 测试注入 FakeLLM；生产留空走真实 DeepSeek
        out: Optional[_Out] = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.provider = provider
        self.model = model or os.environ.get("EMBERPY_MODEL") or "deepseek-chat"
        self.base_url = base_url or os.environ.get("DEEPSEEK_BASE_URL")
        self.api_key = api_key if api_key is not None else (os.environ.get("DEEPSEEK_API_KEY") or "")
        # 会话文件（绝对路径）；None = 还没落盘（纯内存 / 或待首个 prompt 才分配）
        self.session_file: Optional[str] = None
        # 是否有当前会话的 Session 对象（含已从磁盘加载的历史）
        self.session: Optional[Session] = None
        # 桌面端会设 EMBER_SESSIONS_DIR/EMBER_HOME 给子进程；有它 = 桌面场景，
        # 新会话要在首个 prompt 自动分配会话文件（测试/纯库用法没有这个 env，保持内存）
        self.persist = _desktop_sessions_known()
        self.injected_llm = llm
        self.out = out or _Out()
        self._sandbox = sandbox  # 记住沙箱，/permissions 切换时按 (新权限, 沙箱) 重算生效模式
        self.mode = effective_mode(permission, sandbox)
        # 进入 plan 前记住要恢复的模式（批准 /plan execute 时回到它）
        self._mode_before_plan: Optional[PermissionMode] = None
        self.session_id = uuid.uuid4().hex[:12]
        # 桌面场景（有 EMBER_HOME/EMBER_SESSIONS_DIR env）启用跨会话记忆：
        # 记忆库只在此刻解析路径，文件/目录首次 memory_save 才真正创建（不产生空目录）。
        # 测试想隔离记忆目录就设 EMBERPY_MEMORY_DIR 到临时目录。
        self.memory: Optional[MemoryStore] = MemoryStore(resolve_memory_dir(self.workspace)) if self.persist else None
        # 技能：只在桌面场景发现（扫 workspace 附近 .agents/.claude/skills + home 下
        # 同名字目录，见 resolve_skill_roots）；没有任何技能时保持 None（工具集/命令面不变）。
        # 测试/嵌入隔离：EMBERPY_SKILLS_DIR 指到临时目录。
        self.skills: Optional[SkillStore] = self._load_skills() if self.persist else None
        # 自定义 agent 定义（R-E）：只在桌面场景发现 .claude/agents / .ember/agents 下的
        # *.md；没有任何定义时保持 None（run_agent 只有内置 general/explore）。
        # 测试/嵌入隔离：EMBERPY_AGENTS_DIR 指到临时目录。
        self.agents: Optional[AgentStore] = self._load_agents() if self.persist else None
        # hooks：只在桌面场景装配（persist）——读用户级 <home>/.ember/hooks.json（恒读）
        # + 项目级 <workspace>/.ember/hooks.json（EMBERPY_PROJECT_HOOKS=1 才并入）。
        # 非桌面/纯库不读真实 ~/.ember，避免测试与嵌入被本机用户配置意外干扰。
        self.hooks: Optional[HookManager] = load_hook_manager(self.workspace) if self.persist else None
        # 权限策略：deny 规则（env EMBERPY_DENY 恒读 + 项目 .ember/permissions.json，
        # 仅 EMBERPY_PROJECT_PERMISSIONS=1 时并入）+ 内置敏感集合。装配一次跨
        # agent/子 agent 共享，deny 不能被下层绕过。load 只读项目内配置文件、不写盘，
        # 无副作用，桌面与纯库都用默认空 deny（敏感集合仍常开）。
        self.policy: PermissionPolicy = load_permission_policy(self.workspace)
        # 当前会话是否已发过 SessionStart（避免对从未落盘的会话发 SessionEnd）
        self._session_started = False
        # MCP：唯一开关是 EMBERPY_MCP_CONFIG env（指向 JSON 配置文件）。没设 =
        # 整体无 MCP（不 spawn 任何进程）。连接是惰性的：首个用到它的 agent
        # registry 构建时才 spawn+握手（见 tools/mcp_tools build_mcp_tools）。
        self.mcp: Optional[McpManager] = None
        self._mcp_notes: list[str] = []
        self.mcp, self._mcp_notes = manager_from_env(str(self.workspace))

        self.transcript = Transcript()
        self._agent: Optional[Agent] = None
        self._thinking_level: Optional[str] = None
        self._last_checkpoint_seq = 0
        # 自动上下文压缩（claude autoCompact 对齐）：默认关，桌面端 startAgent 时
        # 会发 set_auto_compaction {enabled:true} 打开；测试/纯库不受影响。
        self.auto_compact_enabled = False
        # 自动压缩连续失败熔断（连败≥3 该会话不再自动重试，避免每轮烧模型调用）
        self._compact_failures = 0
        # 回合末记忆自动抽取（#10）：已喂过抽取的本轮补丁水位（见 _new_session 归零）。
        # 每次抽取成功后抬到 agent.patches.high_water，下次只提炼新改动。
        self._auto_mem_seen_patch_seq = 0
        # 模型上下文窗：无 usage 阶段用 env 覆盖或默认 64k（deepseek）粗算触发线
        self._context_window = _compact.env_int("EMBERPY_CONTEXT_WINDOW", 64000)

        # 运行控制（都由主线程改写，运行线程只读）
        self._stop = threading.Event()
        self._steers: deque[str] = deque()
        self._run_thread: Optional[threading.Thread] = None
        self._active_prompt_id: Optional[str] = None

        # UI 确认对话框（extension_ui_request/response 往返，线程安全）
        self.ui = UiConfirm(self.out)

        # 当前"窗口"（一次用户消息到一次助手回答结束之间）的流式现场
        self._open = False
        self._window_texts: list[str] = []
        self._last_error: Optional[str] = None
        # 流式突发现场：一次模型调用的 delta 从第一个可见增量到最终 assistant 事件之间为 True。
        # 期间 delta 文本以"尾部段覆盖"方式写 _window_texts[-1]，收尾（_handle_assistant）把
        # 该段替换成权威最终文本再发一次累计 message_update，与"非流式整包"收敛到同一窗口。
        self._streaming = False

        # 恢复：给了 --session 就立刻打开磁盘历史并重建转录，
        # 让 snapshot()/get_messages 在用户发消息前就返回完整历史
        if session_file:
            self._open_session(Path(session_file))

    # ------------------------------------------------------------------
    # 命令分派（主线程）
    # ------------------------------------------------------------------
    def handle_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        method = msg.get("type")
        if not isinstance(method, str):
            return
        req_id = msg.get("id")
        if method == "extension_ui_response":
            # 权限确认框的回执：无 request 语义，直接路由给等待者
            if isinstance(req_id, str):
                self.ui.resolve(req_id, msg)
            return
        if not isinstance(req_id, str):
            return  # 只响应带 id 的命令
        params = {k: v for k, v in msg.items() if k not in ("type", "id")}
        try:
            data = self._dispatch(method, params, req_id)
        except Exception as exc:  # 任何命令异常都变成错误响应，不让主进程挂起
            self.out.emit({"type": "response", "id": req_id, "success": False, "error": str(exc)})
            return
        if data is _ASYNC:
            return  # 异步命令：稍后由运行线程回响应
        self.out.emit({"type": "response", "id": req_id, "success": True, "data": data})

    def _dispatch(self, method: str, params: dict[str, Any], req_id: str) -> Any:
        if method == "get_state":
            return self._get_state()
        if method == "get_messages":
            return {"messages": self.transcript.get_messages()}
        if method == "get_entries":
            return {"entries": self.transcript.get_entries()}
        if method == "get_session_stats":
            return self._get_stats()
        if method == "get_available_models":
            return {"models": self._available_models()}
        if method == "get_available_thinking_levels":
            return {"levels": ["off", "low", "medium", "high"]}
        if method == "get_commands":
            # 命令面：有技能就返回（source:"skill" 等），桌面端 parseSkillCommands 会收进 UI；
            # 其它命令（/undo /stats 等）是 CLI/渲染层本地逻辑，不属于引擎命令面。
            return {"commands": self.skills.commands() if self.skills else []}
        if method == "get_fork_messages":
            return {"messages": self.transcript.get_messages()}
        if method == "prompt":
            if params.get("images"):
                raise ValueError("emberpy 引擎暂不支持图片输入")
            return self._start_prompt(params.get("message", ""), req_id)
        if method == "abort":
            self._stop.set()
            return {"ok": True}
        if method == "steer":
            text = params.get("message", "")
            if isinstance(text, str) and text.strip():
                self._steers.append(text)
            return {"ok": True}
        if method == "new_session":
            return self._new_session()
        if method == "set_model":
            model_id = params.get("modelId") or params.get("model") or params.get("id")
            if isinstance(model_id, str) and model_id:
                self.model = model_id
                # 让"等级=模型开关"始终一致：模型明确切到聊天/reasoner 时回写等级
                if model_id == "deepseek-chat":
                    self._thinking_level = "off"
                elif model_id == "deepseek-reasoner" and (self._thinking_level is None or self._thinking_level == "off"):
                    self._thinking_level = "high"
            return {"ok": True}
        if method == "set_thinking_level":
            level = params.get("level")
            if isinstance(level, str) and level:
                # 等级驱动模型（off→deepseek-chat，其余→deepseek-reasoner），见 _resolve_model
                self._thinking_level = level
            return {"ok": True}
        if method == "set_auto_compaction":
            # renderer 每次 startAgent 都发 {enabled:true}；本命令只存开关，真正
            # 的触发在每轮 prompt 开始前（_auto_compact_needed -> _compact_history）
            self.auto_compact_enabled = bool(params.get("enabled"))
            return {"ok": True}
        if method == "compact":
            # 手动 /compact：renderer 已保证不在运行中；同步压缩一次。
            # 对话太短时抛 "nothing to compact..."（renderer 正则匹配转 toast）。
            # result 恒有 tokensBefore（renderer toast 显示压缩前 token 数）。
            return self._compact_history(auto=False) or {"tokensBefore": 0}
        if method == "fork":
            return self._fork_session()
        raise ValueError(f"未知命令：{method}")

    def _resolve_model(self) -> str:
        """思考等级 = 模型开关：等级 off 用聊天模型，其余用 reasoner。

        等级还没同步（UI 没下发，例如无界面直接跑）时跟随显式模型选择，
        不改变默认行为。
        """
        level = self._thinking_level
        if level is None:
            return self.model
        return "deepseek-chat" if level == "off" else "deepseek-reasoner"

    def _get_state(self) -> dict[str, Any]:
        running = self._run_thread is not None and self._run_thread.is_alive()
        state: dict[str, Any] = {
            "isStreaming": running,
            "mode": self.mode.value,
            "sessionId": self.session_id,
            "model": self._resolve_model(),
        }
        if self.session_file:
            state["sessionFile"] = self.session_file
        if self._thinking_level is not None:
            state["thinkingLevel"] = self._thinking_level
        return state

    def _get_stats(self) -> dict[str, Any]:
        c = self.transcript.count()
        stats: dict[str, Any] = {
            "sessionId": self.session_id,
            "userMessages": c["users"],
            "assistantMessages": c["assistants"],
            "toolCalls": c["tool_calls"],
            "toolResults": c["tool_results"],
            "totalMessages": c["users"] + c["assistants"] + c["tool_results"],
            "tokens": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            "cost": 0,
        }
        # agent 跑过真实模型调用才填 usage/cost（否则面板保持空，UI 显示"首次回复后出现"）
        agent = self._agent
        if agent is not None and agent.usage.any_usage:
            stats["tokens"] = agent.usage.tokens_dict()
            stats["cost"] = round(agent.usage.cost, 4)
            context = agent.usage.context_usage(self._context_window)
            if context is not None:
                stats["contextUsage"] = context
        if self.session_file:
            stats["sessionFile"] = self.session_file
        return stats

    def _available_models(self) -> list[dict[str, Any]]:
        window = self._context_window
        models = [
            {"provider": self.provider, "id": "deepseek-chat", "contextWindow": window},
            {"provider": self.provider, "id": "deepseek-reasoner", "contextWindow": window, "reasoning": True},
        ]
        # 当前"实际生效"的模型必须出现在列表里，界面下拉才会正确回显
        effective = self._resolve_model()
        if effective not in [m["id"] for m in models]:
            models.insert(0, {"provider": self.provider, "id": effective, "contextWindow": window})
        return models

    # ------------------------------------------------------------------
    # prompt / abort / steer（运行控制）
    # ------------------------------------------------------------------

    def _start_prompt(self, message: Any, req_id: str) -> Any:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("空 prompt")
        thread = self._run_thread
        if thread is not None and thread.is_alive():
            raise ValueError("已有任务在运行，请先等待或停止")
        # 纯状态命令（/permissions）不建 agent/会话/落盘；真任务在 _execute_prompt 内按需创建
        if parse_permissions_command(message) is None:
            self._ensure_agent()
        # 响应的 id 必须是请求的 id：agent-host 靠它匹配 pending 请求
        self._active_prompt_id = req_id
        runner = threading.Thread(
            target=self._run_prompt_thread,
            args=(message, req_id),
            name="emberpy-run",
            daemon=True,
        )
        self._run_thread = runner
        runner.start()
        return _ASYNC

    def _run_prompt_thread(self, message: str, prompt_id: str) -> None:
        """运行线程：回显用户消息 -> 跑 agent 循环 -> 收尾事件 + 回 prompt 响应。"""
        try:
            self._execute_prompt(message)
            self.out.emit({"type": "response", "id": prompt_id, "success": True, "data": {}})
        except Exception as exc:  # 运行期兜底：不吞掉，把错误变成一条助手消息
            self._emit_error(str(exc))
            self.out.emit({"type": "response", "id": prompt_id, "success": True, "data": {}})
        finally:
            self._run_thread = None
            self._active_prompt_id = None
            self._stop.clear()
            self._close_window()
            self.out.emit({"type": "queue_update", "steering": []})
            self.out.emit({"type": "agent_settled"})

    def _apply_permission_mode(self, mode_str: str) -> PermissionMode:
        """把 /permissions 的权限切换落地到 worker 与（若已建的）agent。

        进 plan 前记下当前模式；离开 plan（切到任何其它模式）时清掉记忆。
        """
        new = effective_mode(mode_str, self._sandbox)
        prev = self.mode
        self.mode = new
        if new is PermissionMode.PLAN and prev is not PermissionMode.PLAN:
            self._mode_before_plan = prev
        elif new is not PermissionMode.PLAN:
            self._mode_before_plan = None
        if self._agent is not None:
            self._agent.set_mode(new)
        return new

    def _leave_plan_mode(self) -> None:
        """/plan execute：退出计划模式，恢复到批准前（没有则 auto）。"""
        if self.mode is not PermissionMode.PLAN:
            return
        target = self._mode_before_plan or PermissionMode.AUTO
        self._apply_permission_mode(target.value)

    def _execute_prompt(self, message: str):
        """同步执行一轮 prompt（运行线程调用；测试可直接调用）。"""
        self._last_error = None
        self._close_window()  # 上一轮若还开着先收尾
        # 引擎级文本命令：/permissions 切权限模式——纯状态变更，不建 agent、不落盘
        mode_str = parse_permissions_command(message)
        if mode_str is not None:
            self._echo_user(message)
            self._emit_assistant_text(_permission_notice(self._apply_permission_mode(mode_str)))
            return None
        # /plan execute：先退出计划模式（恢复批准前权限），再让 agent 开跑
        if is_plan_execute_command(message):
            self._leave_plan_mode()
            task = PLAN_EXECUTE_TASK
        else:
            task, skill_error = self._expand_skill_message(message)
            if skill_error is not None:
                # 技能名不存在：直接把可用列表回给用户，不起 agent（省一次模型调用）
                self._echo_user(message)
                self._emit_assistant_text(skill_error, append=False)
                return None
        # UserPromptSubmit hook：可拦截本轮（不跑 agent）或用 modifiedPrompt 改写。
        # 放 _ensure_agent 之前：拦截时干净收场，不建 agent/不开新会话文件。
        ups = self._fire_hooks("UserPromptSubmit", {"prompt": message})
        if ups.blocked_reason is not None:
            self._echo_user(message)
            self._emit_assistant_text(
                f"⚠ 输入被 UserPromptSubmit hook 拦截：{ups.blocked_reason}", append=False
            )
            return None
        modified_prompt = ups.modified_prompt or None
        self._ensure_agent()
        # 自动压缩（桌面 autoCompactEnabled）：本轮开始前把超预算旧历史归纳成摘要。
        # 放在回显/追加新 user 之前——压缩会重建转录，若先 echo 新用户就被冲掉了；
        # 压缩完再 echo，界面看到的顺序仍是「(摘要)…用户本轮输入」。
        if not is_plan_execute_command(message) and self._auto_compact_needed():
            self._compact_history(auto=True)
        # 回显用户消息：界面用它替换输入框里的乐观气泡（压缩可能已重建过转录）
        self._echo_user(message)
        try:
            # 实际喂给 agent 的文本：技能展开的任务优先；否则 hook 改写后的 prompt；
            # 都没有就用用户原话。注意 transcript 里保留的是原话（界面所见），会话历史
            # 落的是 run 文本（模型所见），与技能展开的既有语义一致。
            run_text = task if task is not None else (modified_prompt or message)
            result = self._agent.run(run_text)
        except Exception as exc:  # 引擎同步崩溃不应杀死 worker
            self._last_error = str(exc)
            result = None
        if self._last_error:
            # 模型调用失败（缺 key / 网络 / 鉴权等），必须让界面看到原因
            self._emit_error(f"⚠ 模型调用失败：{self._last_error}")
        self._close_window()
        # 回合末静默提炼记忆（#10）：仍在 run 线程内、response success 之前同步跑，
        # 保持"单任务"不变量（无后台并发写索引）；自吞异常，不影响主流程。
        self._auto_extract_memories(result)
        return result

    def _auto_extract_memories(self, result) -> None:
        """回合末自动抽取记忆（#10，对齐 claude extractMemories 的单次调用版）。

        只走 worker 侧一次非流式 LLM 调用（tools=None），引擎侧 parse/去重/校验后
        逐条 MemoryStore.save —— 不建子 agent、不额外开线程。调用**不计入** agent
        usage/面板（与 compact 摘要先例一致）。任何失败都自吞，绝不外抛。
        """
        try:
            if self.memory is None:
                return  # 非桌面/未启用记忆
            if not _extract.auto_memory_enabled():
                return  # env 门控默认关（EMBERPY_AUTO_MEMORY）
            if self._agent is None or self.session is None or result is None:
                return
            if self._last_error:
                return  # 本轮模型调用失败/引擎崩溃，不提炼
            # 本轮事件切片 = 最后一个 user（含当前任务）到结尾
            events = self.session.events()
            cut = -1
            for i in range(len(events) - 1, -1, -1):
                if events[i].get("type") == "user":
                    cut = i
                    break
            if cut < 0:
                return
            turn = events[cut:]
            task = (turn[0].get("message") or {}).get("content") or ""
            final = (result.final_content or "").strip()
            patches = self._agent.patches.since_seq(self._auto_mem_seen_patch_seq)
            names, errors = _extract.tool_digest(turn)
            if not _extract.should_extract_turn(
                final_text=final, patch_count=len(patches), tool_count=len(names)
            ):
                return  # 琐碎寒暄轮，跳过
            _extract.run_memory_extraction(
                self._make_llm(),
                self.memory,
                task=task,
                final=final,
                patch_digest=_extract.patch_lines(patches, self.workspace),
                tool_digest=_extract.tool_trail(names, errors),
            )
            # 本轮改动已喂过抽取：水位抬到已发出的最大补丁序号，下次只提炼新改动
            self._auto_mem_seen_patch_seq = self._agent.patches.high_water
        except Exception:
            pass  # 提炼失败绝不影响主流程

    def _echo_user(self, message: str) -> None:
        """把用户消息写进转录并发 message_start（界面替换输入框的乐观气泡）。"""
        record = self.transcript.add_user(message)
        self.out.emit({"type": "message_start", "message": record})

    def _expand_skill_message(self, message: str) -> tuple[Optional[str], Optional[str]]:
        """把 ``/skill:名 [参数]``（或 <skill name=…> 块）展开成技能正文当任务。

        返回 ``(task, error)``：task 非 None 就用它当任务跑（正文里的 $ARGUMENTS
        已替换成参数）；否则 error 是要直接回给用户的文本（技能不存在）。与技能无关
        的普通消息返回 (None, None)。self.skills 为 None（未启用）时也不处理。
        """
        if self.skills is None:
            return None, None
        m = _SKILL_SLASH_RE.match(message)
        name: Optional[str] = None
        args = ""
        if m:
            name = m.group(1)
            args = (m.group(2) or "").strip()
        else:
            b = _SKILL_BLOCK_RE.match(message)
            if b:
                name = b.group(1).strip()
                args = (b.group(2) or "").strip()
        if not name:
            return None, None
        skill = self.skills.get(name)
        if skill is None:
            available = "、".join(self.skills.names()) or "（无）"
            return None, f"⚠ 没有技能 “{name}”。可用技能：{available}"
        return self.skills.render(skill, args), None

    def _emit_error(self, text: str) -> None:
        if self._window_texts:
            self._emit_assistant_text(f"\n{text}", append=True)
        else:
            self._emit_assistant_text(text, append=False)

    # ------------------------------------------------------------------
    # 运行线程用的外部输入回调
    # ------------------------------------------------------------------

    def _drain_steers(self) -> list[str]:
        items: list[str] = []
        while self._steers:
            items.append(self._steers.popleft())
        return items

    def _on_user_injected(self, text: str) -> None:
        """一个 steer 变成 user 消息时（agent 循环里、运行线程上）被调用。"""
        self._close_window()  # 先收掉正在流的助手气泡
        record = self.transcript.add_user(text)
        self.out.emit({"type": "message_start", "message": record})
        self.out.emit({"type": "queue_update", "steering": list(self._steers)})

    # -- 流式窗口辅助 ------------------------------------------------------

    def _emit_assistant_text(self, text: str, append: bool = False, thinking: Optional[str] = None) -> None:
        """发一条助手消息事件（可选 thinking part + 本窗口累计全文）。

        append=True：把 text 并入窗口最后一段（错误行/同一回复追加）；
        否则另起一段。引擎每轮内容就是该轮的完整文本，正常一条文本=一段。
        thinking 只带本轮新增的推理段：renderer 的 joinThinking 按空行分段的
        "包含/前缀"去重追加，重复发也安全；text 必须带累计全文（整文本覆盖）。
        """
        if text:
            if append and self._window_texts:
                self._window_texts[-1] = text
            else:
                self._window_texts.append(text)
        cumulative = "\n".join(self._window_texts)
        content: list[dict[str, Any]] = []
        if thinking:
            content.append(thinking_part(thinking))
        if cumulative:
            content.append(text_part(cumulative))
        msg = pi_message("assistant", content)
        self.out.emit({"type": "message_start", "message": msg})
        self._open = True

    def _close_window(self) -> None:
        """收尾当前窗口：助手气泡还开着就发 message_end 停止流式动画。"""
        if not self._open:
            return
        cumulative = "\n".join(self._window_texts)
        msg = pi_message("assistant", [text_part(cumulative)])
        self.out.emit({"type": "message_end", "message": msg})
        self._open = False
        self._window_texts = []
        self._streaming = False

    # ------------------------------------------------------------------
    # 引擎装配
    # ------------------------------------------------------------------

    def _load_skills(self) -> Optional[SkillStore]:
        """发现技能根并建 store；没发现任何技能返回 None（保持默认工具集/命令面）。

        workspace 传进去供 paths 条件技能的相对路径匹配（R-F）。"""
        store = SkillStore(resolve_skill_roots(self.workspace), workspace=self.workspace)
        return store if store.all() else None

    def _load_agents(self) -> Optional[AgentStore]:
        """发现自定义 agent 定义（R-E）；没有任何定义返回 None（run_agent 只内置两种）。"""
        store = AgentStore(resolve_agent_roots(self.workspace))
        return store if store.all() else None

    def _make_llm(self) -> LLM:
        if self.injected_llm is not None:
            return self.injected_llm
        return ChatLLM(
            api_key=self.api_key,
            model=self._resolve_model(),
            base_url=self.base_url or "https://api.deepseek.com",
            timeout=180.0,
        )

    def _ensure_session(self) -> None:
        """确保当前会话的 Session 对象存在（首次真正需要写内容前调用）。

        桌面场景（persist）首个 prompt 才惰性分配会话文件，避免空文件污染侧栏；
        测试/纯库用法退回纯内存。resume 场景 __init__ 已建好，这里直接返回。
        """
        if self.session is not None:
            return
        if self.persist:
            path = allocate_session_path()
            self.session_file = str(path)
            self.session = Session(path=path, cwd=self.workspace)
        else:
            self.session = Session(cwd=self.workspace)
        self._start_session_hooks()

    def _ensure_agent(self) -> Agent:
        if self._agent is not None:
            return self._agent
        # 会话对象与 Agent 复用同一个：run 会续写历史，落盘/resume 现场一致
        self._ensure_session()
        # env 的 gate/patches 传 None：让 Agent 自建并让 tools 复用同一份，
        # 否则写文件会落进另一套 PatchStore，checkpoint 同步会失效。
        env = ToolEnv(
            workspace=self.workspace,
            gate=None,
            patches=None,
            confirm=self.ui.ask,
            ask_value=self.ui.select,
            memory=self.memory,
            skills=self.skills,
            agents=self.agents,
            mcp=self.mcp,
        )
        rt = RuntimeInput(
            stop_requested=self._stop.is_set,
            drain_steers=self._drain_steers,
            on_user_injected=self._on_user_injected,
        )
        agent = Agent(
            llm=_DynamicLLM(self._make_llm) if self.injected_llm is None else self.injected_llm,
            workspace=self.workspace,
            mode=self.mode,
            env=env,
            session=self.session,
            on_event=self._on_agent_event,
            runtime_input=rt,
            allow_subagents=True,  # 桌面主 agent 可派生子 agent（子 agent 自己不能再派生）
            hooks=self.hooks,  # PreToolUse/Stop 等命令 hook（无配置即 no-op）
            policy=self.policy,  # deny + 敏感集合（子 agent 共享同一 gate -> 绕不过）
        )
        self._agent = agent
        return agent

    # -- hooks（worker 侧事件：会话生命周期 / 用户输入 / 压缩） ---------------
    def _fire_hooks(self, event: str, payload: dict[str, Any]):
        """跑某事件的全部 worker 侧 command hook（无配置即 no-op），返回聚合结果。

        Agent 内部的工具/子代理/Stop hook 由 agent 自触发；这里只管 worker 层事件：
        SessionStart/End、UserPromptSubmit、PreCompact/PostCompact。
        """
        manager = self.hooks
        if manager is None or not manager.has(event):
            return HookRunOutcome(event=event)
        extra_env: dict[str, str] = {
            "HOOK_EVENT": event,
            "CWD": str(self.workspace),
            "PERMISSION_MODE": self.mode.value,
        }
        if self.session_file:
            extra_env["TRANSCRIPT_PATH"] = self.session_file
        return manager.run(event, payload, cwd=str(self.workspace), extra_env=extra_env)

    def _start_session_hooks(self) -> None:
        """当前会话对象建好后（首个 prompt 或 resume 打开）触发 SessionStart。"""
        if self.hooks is None or self._session_started:
            return
        self._fire_hooks(
            "SessionStart",
            {"session_id": self.session_id, "cwd": str(self.workspace), "session_file": self.session_file},
        )
        self._session_started = True

    def _end_session_hooks(self) -> None:
        """会话结束（新建会话/关闭）前触发 SessionEnd，并复位标志防重复。"""
        if self.hooks is None or not self._session_started:
            return
        self._fire_hooks("SessionEnd", {"session_id": self.session_id, "cwd": str(self.workspace)})
        self._session_started = False

    def _prune_shell_spill(self) -> None:
        """删掉工作区 .ember/out 里超过 24h 的 shell 落盘日志。

        Feature B（#16 窄版）：run_command 超限输出的全文落到项目级 spill 目录
        （.ember/out），crash 残留 / 跨会话堆积由这里兜底
        （本次会话内的文件保留）。best-effort，失败静默——清理不能影响会话/worker 主流程。
        """
        try:
            cutoff = time.time() - 24 * 3600
            out_dir = Path(self.workspace) / ".ember" / "out"
            if not out_dir.is_dir():
                return
            for f in out_dir.iterdir():
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _new_session(self) -> dict[str, Any]:
        self._prune_shell_spill()
        self._stop.set()
        thread = self._run_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        # 当前会话收尾（若有）先发 SessionEnd
        self._end_session_hooks()
        self._agent = None
        self.transcript.reset()
        self.session_id = uuid.uuid4().hex[:12]
        self._last_checkpoint_seq = 0
        self._auto_mem_seen_patch_seq = 0  # 新会话的 agent PatchStore 序号从 0 重新计
        self._open = False
        self._window_texts = []
        self._streaming = False
        # 新会话换一块干净 Session（桌面场景下个 prompt 才分配新文件，见 _ensure_session）
        self.session = None
        self.session_file = None
        self._stop.clear()
        return {"ok": True}

    def close(self) -> None:
        """释放外部资源（MCP 子进程）。stdin 结束后由 main 调用，幂等。"""
        self._prune_shell_spill()
        self._end_session_hooks()
        if self.mcp is not None:
            self.mcp.close()
            self.mcp = None

    # ------------------------------------------------------------------
    # agent 事件 -> renderer 事件 / 转录
    # ------------------------------------------------------------------

    def _on_agent_event(self, type_: str, data: dict[str, Any]) -> None:
        try:
            if type_ == "assistant":
                self._handle_assistant(data)
            elif type_ == "assistant_delta":
                self._handle_assistant_delta(data)
            elif type_ == "tool_call":
                self._handle_tool_call(data)
            elif type_ == "tool_result":
                self._handle_tool_result(data)
            elif type_ == "model_error":
                self._last_error = str(data.get("error", "模型调用失败"))
            # user_injected 已通过 on_user_injected 处理，忽略重复事件
        except Exception:
            pass  # 事件观察者不应影响 agent 运行

    def _assistant_record(self, raw: dict[str, Any]) -> tuple[str, list[dict[str, Any]], Optional[str]]:
        """把一个 assistant 轮次拆成转录所需三元组 (text, tool_parts, thinking)。

        text 是纯文本 content；tool_parts 是 toolCall 内容块（历史重载时显示轨迹）；
        thinking 是 reasoning_content（DeepSeek reasoner）。live 与 resume replay 共用。
        """
        content = raw.get("content")
        text = content if isinstance(content, str) else ""
        tool_parts: list[dict[str, Any]] = []
        for call in raw.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            fn = fn if isinstance(fn, dict) else {}
            args_raw = fn.get("arguments", "{}")
            try:
                arguments = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except json.JSONDecodeError:
                arguments = {}
            tool_parts.append(tool_part(str(call.get("id", "")), str(fn.get("name", "")), arguments))
        reasoning = raw.get("reasoning_content")
        thinking = reasoning.strip() if isinstance(reasoning, str) and reasoning.strip() else None
        return text, tool_parts, thinking

    def _handle_assistant_delta(self, data: dict[str, Any]) -> None:
        """流式增量（agent 把模型 delta 转成 assistant_delta 事件）。

        每次拿到的是"本段累计 text / 累计 thinking"。策略：突发第一个可见增量
        在 _window_texts 末尾新开一段并发 message_start；后续增量覆盖该段并发
        message_update（携带累计全文 + thinking）。与整包路径收敛的关键：
        _handle_assistant（最终 assistant 事件）会把这段替换成权威最终文本再
        补一次 message_update，因此窗口/事件最终形状与非流式一致。
        """
        text = data.get("text")
        thinking = data.get("thinking")
        text = text if isinstance(text, str) else ""
        thinking = thinking.strip() if isinstance(thinking, str) and thinking.strip() else None
        if not text and not thinking:
            return  # 纯工具增量/空增量：没有可见内容，等最终 assistant 事件
        if not self._streaming:
            # 一段新助手的首个可见增量：新开一段文本并发 message_start 开气泡
            self._streaming = True
            self._window_texts.append(text)
            self._open = True
            self._emit_stream("message_start", thinking)
        else:
            # 已在流式：只覆盖末尾段（保持"累计全文"语义）
            self._window_texts[-1] = text
            self._emit_stream("message_update", thinking)

    def _emit_stream(self, event_type: str, thinking: Optional[str]) -> None:
        """按当前窗口累计文本发一条 assistant 消息事件（message_start/update 共用）。"""
        cumulative = "\n".join(self._window_texts)
        content: list[dict[str, Any]] = []
        if thinking:
            content.append(thinking_part(thinking))
        if cumulative:
            content.append(text_part(cumulative))
        self.out.emit({"type": event_type, "message": pi_message("assistant", content)})

    def _handle_assistant(self, raw: dict[str, Any]) -> None:
        text, tool_parts, thinking = self._assistant_record(raw)
        # 转录每条助手轮次；界面实时由消息事件驱动（thinking+text）
        self.transcript.add_assistant(text, tool_parts, thinking=thinking)
        if self._streaming:
            # 流式突发收尾：把尾部段替换成权威最终文本再发一次累计 message_update。
            # 若最终没有文本（纯 thinking/工具），尾部占位段收掉——thinking 增量已展示过，
            # 这里把它并入累计 message 再对齐一次（renderer 整文本覆盖/thinking 去重幂等）。
            if text:
                self._window_texts[-1] = text
            elif self._window_texts and not self._window_texts[-1]:
                self._window_texts.pop()
            self._streaming = False
            self._emit_stream("message_update", thinking)
        elif text or thinking:
            # 非流式（FakeLLM 整包）：发 message_start，thinking+text 一次性给全
            self._emit_assistant_text(text, thinking=thinking)
        else:
            self._open = True  # 纯工具轮：确保窗口在收尾时能正常关闭

    def _handle_tool_call(self, data: dict[str, Any]) -> None:
        self._open = True
        self.out.emit(
            {
                "type": "tool_execution_start",
                "toolName": data.get("name", "tool"),
                "toolCallId": str(data.get("id", "")),
                "args": data.get("arguments") or {},
                "timestamp": now_ms(),
            }
        )

    def _handle_tool_result(self, data: dict[str, Any]) -> None:
        self._open = True
        tool_call_id = str(data.get("id", ""))
        result_text = data.get("result")
        if not isinstance(result_text, str):
            result_text = str(result_text)
        # denied 标志（R-G③）：工具层不再把权限拒绝吞成普通文本，Denied 由
        # agent._execute 上抛转成 denied=True 的工具结果，这里据此标 isError。
        denied = data.get("denied") is True
        is_error = denied or _is_error_result(result_text)
        self.transcript.add_tool_result(tool_call_id, is_error, result_text)
        self.out.emit(
            {
                "type": "tool_execution_end",
                "toolName": data.get("name", "tool"),
                "toolCallId": tool_call_id,
                "isError": is_error,
                "result": result_text,
                "timestamp": now_ms(),
            }
        )
        self._sync_checkpoints()

    def _sync_checkpoints(self) -> None:
        """把引擎的写文件补丁增量转成界面 ember-checkpoint 条目（供 /undo）。"""
        agent = self._agent
        if agent is None:
            return
        for patch in agent.patches.since_seq(self._last_checkpoint_seq):
            self._last_checkpoint_seq = patch.seq
            rel = _workspace_rel(self.workspace, patch.path)
            self.transcript.add_checkpoint(str(patch.seq), [{"path": rel, "content": patch.before}])

    # ------------------------------------------------------------------
    # 上下文压缩（compact，对齐 claude autoCompact / /compact）
    # ------------------------------------------------------------------

    def _auto_compact_needed(self) -> bool:
        """本轮 prompt 开始前是否要先自动压缩（proactive，对齐 claude）。"""
        if not self.auto_compact_enabled:
            return False
        # 计划模式只读调研、历史通常不长：别在用户批准前偷偷压掉只读调研上下文
        if self.mode is PermissionMode.PLAN:
            return False
        # 熔断：连败 ≥3 说明上下文不可救（如 prompt_too_long），别每轮都烧模型重试
        if self._compact_failures >= 3:
            return False
        session = self.session
        if session is None:
            return False
        threshold = _compact.auto_compact_threshold(self._context_window)
        return _compact.should_auto_compact(session.resume_messages(), threshold)

    def _compact_history(self, *, auto: bool) -> Optional[dict[str, Any]]:
        """执行一次压缩：旧段归纳成摘要、替换历史并重写会话文件/转录。

        auto=True（自动路径）：旧段过小/无旧段/摘要失败都静默返回 None，不打扰用户；
        auto=False（/compact 手动）：太短抛 "nothing to compact"（renderer 正则转
        compactTooShort toast），失败原样抛给 renderer 显示。
        返回 {"tokensBefore", "summary", "messagesSummarized"}。
        """
        session = self.session
        if session is None:
            if auto:
                return None
            raise ValueError("nothing to compact: no active session")
        old_events, keep_events = _compact.split_old_events(session.events())
        if not old_events:
            if auto:
                return None
            raise ValueError("nothing to compact: conversation is still too short")
        segment = _compact.old_segment_messages(old_events)
        tokens_before = _compact.estimate_messages_tokens(segment) if segment else 0
        if not segment or tokens_before < _compact.min_compact_tokens():
            if auto:
                return None
            raise ValueError("nothing to compact: conversation is still too short")
        # PreCompact hook（确认有东西可压后触发；无配置即 no-op）
        self._fire_hooks("PreCompact", {"auto": auto, "tokens_before": tokens_before})
        try:
            summary = _compact.summarize(self._make_llm(), segment)
        except Exception as exc:
            if auto:
                # 摘要失败按一次失败累计熔断（成功会清零）
                self._compact_failures += 1
                return None
            raise ValueError(f"压缩失败：{exc}") from exc
        removed = len(segment)
        marker = _compact.summary_text(summary, removed, tokens_before)
        new_events = [{"type": "assistant", "message": {"role": "assistant", "content": marker}}] + keep_events
        session.replace_events(new_events)
        # 重建转录：新 events（摘要 + 保留尾）replay 回界面视角；被压段的旧
        # checkpoint 随之丢弃（压缩即放弃那部分细粒度 undo，语义一致）。
        self.transcript = Transcript()
        self._replay_session()
        # 抬高同步水位：被压段里的 checkpoint 已从事件里删除，若水位仍低，
        # _sync_checkpoints 会把 agent 内存里同批旧补丁重放进新转录。抬到 agent
        # 已发出的最大序号即可避免（后续新写补丁序号更高，照常同步）。
        if self._agent is not None:
            self._last_checkpoint_seq = max(self._last_checkpoint_seq, self._agent.patches.high_water)
        # PostCompact hook（压缩成功落地后触发）
        self._fire_hooks(
            "PostCompact",
            {"auto": auto, "tokens_before": tokens_before, "messages_summarized": removed},
        )
        if auto:
            self._compact_failures = 0
        return {"tokensBefore": tokens_before, "summary": summary, "messagesSummarized": removed}

    # ------------------------------------------------------------------
    # 会话生命周期：打开磁盘历史 / resume replay / fork
    # ------------------------------------------------------------------

    def _open_session(self, path: Path) -> None:
        """按给定会话文件打开历史并重建转录（resume）。

        传进来的可能是桌面端分区（YYYY/MM/DD 下硬链）路径，先 resolve 成绝对路径；
        文件不存在或为空会被 Session 当成新会话（写 v2 头）——桌面端 resume 都指向已有文件。
        """
        path = Path(path).expanduser().resolve()
        self.session_file = str(path)
        self.session = Session(path=path, cwd=self.workspace)
        self._replay_session()
        self._start_session_hooks()  # resume 打开即有活动会话 -> SessionStart

    def _replay_session(self) -> None:
        """把已加载的磁盘历史重放进转录，让界面在发消息前就看到完整历史。

        逐事件映射（Session 事件 -> 界面 transcript）：
          user      -> add_user
          assistant -> _assistant_record 拆成 text/tool_parts/thinking 再 add_assistant
          tool      -> add_tool_result（错误按文本前缀判定）
          patch     -> add_checkpoint（路径换算成工作区相对路径；before=改前全文，/undo 可用）
        """
        session = self.session
        if session is None:
            return
        for event in session.events():
            etype = event.get("type")
            if etype == "user":
                content = (event.get("message") or {}).get("content")
                if isinstance(content, str):
                    self.transcript.add_user(content)
            elif etype == "assistant":
                text, tool_parts, thinking = self._assistant_record(event.get("message") or {})
                self.transcript.add_assistant(text, tool_parts, thinking=thinking)
            elif etype == "tool":
                msg = event.get("message") or {}
                result_text = msg.get("content") or ""
                self.transcript.add_tool_result(str(msg.get("tool_call_id", "")), _is_error_result(result_text), result_text)
            elif etype == "patch":
                patch = event.get("patch") or {}
                rel = _workspace_rel(self.workspace, patch.get("path"))
                self.transcript.add_checkpoint(str(patch.get("seq", 0)), [{"path": rel, "content": patch.get("before")}])
        # 重放的 checkpoint 已直接进转录；resume 后新 agent 的 PatchStore 从 1 重新
        # 编号，水位归零让 _sync_checkpoints 能把新写盘补丁全部同步上来（不重复）。
        self._last_checkpoint_seq = 0

    def _fork_session(self) -> dict[str, Any]:
        """把当前会话复制成一个独立的新会话文件，返回其路径（不切换当前现场）。

        renderer 目前没有 fork 入口（命令在白名单、UI 无消费者），先做引擎层
        最小原语：新文件是当前文件的字节级副本，可被索引器当作独立 thread 列出。
        """
        src = self.session_file
        if not src or not Path(src).exists():
            return {"ok": False, "error": "当前会话还没有落盘文件，无法 fork"}
        new_path = allocate_session_path()
        new_path.write_bytes(Path(src).read_bytes())
        return {"ok": True, "sessionFile": str(new_path)}


# ---------------------------------------------------------------------------
# CLI 入口（供桌面端 spawn：python -m emberpy.rpc）
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emberpy.rpc",
        description="Ember 的 Python agent worker（JSON-RPC over stdio，仅供桌面端 spawn）",
    )
    parser.add_argument("--permission", default="auto", choices=["plan", "ask", "auto", "full"])
    parser.add_argument("--sandbox", default="workspace-write", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--session", default=None)
    parser.add_argument("--cwd", default=None)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # Windows 控制台默认非 UTF-8；协议必须 UTF-8
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()
    worker = Worker(
        workspace=cwd,
        permission=args.permission,
        sandbox=args.sandbox,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        session_file=args.session,
    )
    for line in sys.stdin:
        if not line or not line.strip():
            continue
        worker.handle_line(line.strip())
    worker.close()
    return 0
