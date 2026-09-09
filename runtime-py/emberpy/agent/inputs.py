"""运行中的外部输入（RPC 桥接用，CLI 不需要时留空）。

这是"agent 与宿主解耦"的边界类型：宿主（桌面桥 / 测试）通过它注入
停止请求与插话，agent 循环不知道宿主的实现细节。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class RuntimeInput:
    """- stop_requested: 返回 True 表示外部要求停止（在每步之间检查，不打断正在执行的工具）
    - drain_steers:    取走积压的 steer 消息；会在下一轮模型调用前作为 user 消息插入
    - on_user_injected: 每当一个 steer 变成 user 消息时回调（让上层同步广播到界面）
    """

    stop_requested: Optional[Callable[[], bool]] = None
    drain_steers: Optional[Callable[[], list[str]]] = None
    on_user_injected: Optional[Callable[[str], None]] = None
