"""补丁 / checkpoint / undo 域（对齐 ember-checkpoint 思想）。

错误类型统一在 errors.py 定义，这里只重导出，方便 ``from emberpy.patches import X``。
"""
from ..errors import FileToolError, NotTextFileError
from .store import Patch, PatchStore

__all__ = ["Patch", "PatchStore", "FileToolError", "NotTextFileError"]
