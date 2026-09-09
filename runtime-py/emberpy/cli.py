"""终端入口：python -m emberpy run "任务"  或  python -m emberpy chat。

- run  单轮：跑完即退，适合脚本/测试调用
- chat 交互：多轮对话，支持 /undo 回滚最近一次写文件
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .agent import Agent, RunResult
from .llm import ChatLLM
from .mcp import McpManager, manager_from_env
from .memory import MemoryStore, resolve_memory_dir
from .permission import PermissionMode
from .session import Session
from .tools import ToolEnv

SESSIONS_DIR = Path.home() / ".pyagent" / "sessions"


def _build_llm(args: argparse.Namespace) -> ChatLLM:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        sys.stderr.write(
            "缺少 DEEPSEEK_API_KEY 环境变量。\n"
            "获取地址：https://platform.deepseek.com\n"
            "Windows PowerShell 设置：\n"
            '  $env:DEEPSEEK_API_KEY="sk-..."\n'
            "或永久生效：setx DEEPSEEK_API_KEY \"sk-...\" 后重开终端\n"
        )
        raise SystemExit(2)
    return ChatLLM(
        api_key=api_key,
        model=args.model,
        base_url=args.base_url,
    )


def _confirm_prompt(interactive: bool) -> Any:
    """返回权限门用的 confirm 回调；非交互时返回 None（即 fail closed）。"""
    if not interactive:
        return None

    def ask(prompt: str) -> bool:
        try:
            answer = input(f"{prompt} [y/N] ").strip().lower()
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    return ask


def _print_event(type_: str, data: dict[str, Any]) -> None:
    if type_ == "tool_call":
        name = data.get("name", "?")
        args = data.get("arguments", {})
        # 只打印参数的一行摘要，避免把整段内容刷屏
        summary = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())
        print(f"  [tool] {name}({summary})", flush=True)
    elif type_ == "tool_result":
        if data.get("denied"):
            print(f"  [拒绝] {data.get('preview', '')}", flush=True)
    elif type_ == "model_error":
        print(f"  [模型错误] {data.get('error', '')}", file=sys.stderr, flush=True)


def _make_agent(args: argparse.Namespace, session_path: Optional[Path], confirm: Any) -> Agent:
    llm = _build_llm(args)
    workspace = Path(args.cwd or os.getcwd()).resolve()
    env = ToolEnv(
        workspace=workspace,
        gate=None,  # Agent 会用 mode 重建并覆盖
        patches=None,
        confirm=confirm,
    )
    # CLI 记忆默认关闭（不碰 ~/.ember）；显式设 EMBERPY_MEMORY_DIR 才启用，便于体验
    # 跨会话记忆且测试隔离。
    memory: Optional[MemoryStore] = None
    if os.environ.get("EMBERPY_MEMORY_DIR"):
        memory = MemoryStore(resolve_memory_dir(workspace))
    # MCP 同口径：EMBERPY_MCP_CONFIG 指向配置 JSON 才启用（见 mcp/config.py）
    mcp, _mcp_notes = manager_from_env(str(workspace))
    if mcp is not None:
        env.mcp = mcp  # ToolEnv 是可变 dataclass，直接挂 MCP manager
    return Agent(
        llm=llm,
        workspace=workspace,
        mode=args.mode,
        env=env,
        session=Session(path=session_path, cwd=workspace),
        on_event=_print_event,
        max_steps=args.max_steps,
        memory=memory,
        allow_subagents=True,
    )


def _show_result(result: RunResult) -> None:
    print("", flush=True)
    print(result.final_content or "（模型没有给出文本答复）", flush=True)
    if result.stopped_by_limit:
        print(f"\n[注意：达到最大步数 {result.stats.total_messages} 条消息上限，可能未完成]", flush=True)
    s = result.stats
    print(
        f"\n[统计] 步数 {result.steps} | 用户 {s.user_messages} | "
        f"助手 {s.assistant_messages} | 工具调用 {s.tool_calls} | 写文件 {s.patches}",
        flush=True,
    )


def cmd_run(args: argparse.Namespace) -> int:
    agent = _make_agent(args, session_path=None, confirm=_confirm_prompt(args.interactive))
    try:
        result = agent.run(args.task)
    finally:
        agent.session.close()
    _show_result(result)
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    session_path = args.session
    if session_path is None:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        session_path = SESSIONS_DIR / f"chat-{Path(os.getcwd()).name}-0.jsonl"
    agent = _make_agent(args, session_path=session_path, confirm=_confirm_prompt(True))
    print(f"emberpy chat | 工作目录 {agent.workspace} | 模式 {args.mode} | 会话 {session_path.name}")
    print("输入任务后回车执行；/undo 回滚最近一次写文件；/stats 查看统计；/exit 退出。")
    try:
        while True:
            try:
                text = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue
            if text in ("/exit", "/quit"):
                break
            if text == "/undo":
                print(agent.undo_last_change(), flush=True)
                continue
            if text == "/stats":
                s = agent.session.stats()
                print(f"用户 {s.user_messages} | 助手 {s.assistant_messages} | 工具 {s.tool_calls} | 写文件 {s.patches}")
                continue
            if text.startswith("/"):
                print(f"未知命令：{text}")
                continue
            _show_result(agent.run(text))
    finally:
        agent.session.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="emberpy", description="Python agent 引擎（DeepSeek / OpenAI 兼容）")
    parser.add_argument("--model", default="deepseek-chat", help="模型名（默认 deepseek-chat）")
    parser.add_argument("--base-url", default="https://api.deepseek.com", help="OpenAI 兼容 base_url")
    parser.add_argument("--cwd", default=None, help="工作目录（默认当前目录）")
    parser.add_argument("--mode", choices=[m.value for m in PermissionMode], default="ask", help="权限模式")
    parser.add_argument("--max-steps", type=int, default=40, help="单轮最大工具步数")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="单轮执行一个任务后退出")
    run.add_argument("task", help="要完成的任务")
    run.add_argument("--interactive", action="store_true", help="询问类操作允许读取终端输入")

    chat = sub.add_parser("chat", help="多轮交互对话")
    chat.add_argument("--session", default=None, help="会话文件路径（默认 ~/.pyagent/sessions/ 下自动生成）")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    return cmd_chat(args)


if __name__ == "__main__":
    raise SystemExit(main())
