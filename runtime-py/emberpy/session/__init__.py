"""会话域：JSONL 落盘 + 断点续跑（v2 磁盘持久化的基础层）。
"""
from .core import Session, allocate_session_path, closed_model_messages, resolve_sessions_dir
from .stats import SessionStats

__all__ = ["Session", "SessionStats", "allocate_session_path", "closed_model_messages", "resolve_sessions_dir"]
