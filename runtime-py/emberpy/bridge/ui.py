"""桌面权限确认框的往返（extension_ui_request / extension_ui_response）。

Worker 只认一个 confirm 回调（PermissionGate 需要的形状）；
UiConfirm 把"发请求 + 等回执 + 超时"包成一个线程安全的阻塞 ask()，
运行线程在权限门里调用它，主线程把回执路由回来。
"""
from __future__ import annotations

import threading
from typing import Any, Protocol

UI_TIMEOUT_SECONDS = 600.0


class _Emitter(Protocol):
    """能向桌面端发一行事件的对象（_Out / 测试 sink 都满足）。"""

    def emit(self, obj: dict[str, Any]) -> None: ...


class UiConfirm:
    def __init__(self, out: _Emitter) -> None:
        self._out = out
        self._lock = threading.Lock()
        self._seq = 0
        self._waiters: dict[str, tuple[threading.Event, dict[str, Any]]] = {}

    def ask(self, prompt: str) -> bool:
        """在运行线程里被权限门调用；阻塞等待界面确认框回执。"""
        rid = self._request("confirm", title=prompt)
        return self._wait(rid).get("confirmed") is True

    def select(self, title: str, options: list[str]) -> str | None:
        """单选澄清（AskUserQuestion 落点）：阻塞等用户点一个选项。

        界面 ApprovalCard 的 select 卡片每个选项一个按钮，点选回执
        {value: <选项文本>}；取消回执 {cancelled: true} -> 返回 None。
        """
        rid = self._request("select", title=title, options=list(options))
        result = self._wait(rid)
        if result.get("cancelled"):
            return None
        value = result.get("value")
        return value if isinstance(value, str) and value else None

    def _request(self, method: str, **fields: Any) -> str:
        """登记等待者并发 extension_ui_request，返回 rid。"""
        with self._lock:
            self._seq += 1
            rid = f"ask_{self._seq}"
            done = threading.Event()
            self._waiters[rid] = (done, {})
        payload: dict[str, Any] = {"type": "extension_ui_request", "id": rid, "method": method}
        payload.update(fields)
        self._out.emit(payload)
        return rid

    def _wait(self, rid: str) -> dict[str, Any]:
        """阻塞等待该 rid 的回执；超时视为取消。"""
        with self._lock:
            waiter = self._waiters.get(rid)
        if waiter is None:
            return {}
        done, result = waiter
        done.wait(timeout=UI_TIMEOUT_SECONDS)
        with self._lock:
            self._waiters.pop(rid, None)
        return result

    def resolve(self, rid: str, payload: dict[str, Any]) -> None:
        """主线程把界面的回执路由给正在等待的 ask()。"""
        with self._lock:
            waiter = self._waiters.get(rid)
        if waiter is None:
            return
        done, result = waiter
        for key in ("confirmed", "value", "cancelled"):
            if key in payload:
                result[key] = payload[key]
        done.set()
