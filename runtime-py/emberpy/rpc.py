"""emberpy.rpc —— 桌面 worker 的进程入口（兼容薄壳）。

真实实现在 emberpy/bridge/worker.py。这里只做重导出并保留
``python -m emberpy.rpc`` 这个进程契约（Electron 主进程 src/main/agent-host.ts
按这个名字 spawn；vitest 探针也检查本文件存在），旧代码里
``from emberpy.rpc import Worker, _Out`` 依然可用。
"""
from .bridge.transcript import Transcript
from .bridge.worker import Worker, _Out, build_parser, main

__all__ = ["Worker", "_Out", "Transcript", "build_parser", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
