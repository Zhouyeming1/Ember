"""bridge 域：桌面桥（JSON-RPC over stdio）。

对 claude-code-analysis 里 bridge/ 的角色：宿主（桌面 UI / IDE）与引擎之间
唯一的协议通道。worker.py 是主体；transcript.py 管界面视角的转录；
ui.py 管权限确认框往返。
"""
from .transcript import Transcript
from .worker import Worker, _Out

__all__ = ["Worker", "Transcript", "_Out"]
