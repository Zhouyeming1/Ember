"""skills paths 条件激活测试（R-F）。

覆盖：
- paths 条件技能不进可用清单，模型不可见；无条件技能始终可见；
- activate_for_paths 幂等激活；路径命中后进入 listing/commands/available；
- 规则匹配：无目录文件名规则 / 带 / 锚定规则（src/**）/ ** 任意深度 / ~ 主目录；
- fs 的 read_file/write_file/file_edit 成功后回调激活（read/write 都验证）；
- refresh 后激活状态保留（磁盘技能还在时）；条件技能命令面在激活前隐藏。
"""
from __future__ import annotations

from pathlib import Path

from emberpy.patches import PatchStore
from emberpy.permission import PermissionGate, PermissionMode
from emberpy.skills import SkillStore
from emberpy.tools import ToolEnv, default_registry


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_store(root: Path, workspace: Path) -> SkillStore:
    # 注意：目录式技能的名字 = 相对根的目录名（SKILL.md 里的 name: 不参与命名）
    _write(
        root / "always" / "SKILL.md",
        "---\ndescription: 无条件\n---\n随时可用。",
    )
    _write(
        root / "py" / "SKILL.md",
        "---\ndescription: 碰 py\npaths:\n  - '*.py'\n---\nPython 工作流。",
    )
    _write(
        root / "srcskill" / "SKILL.md",
        "---\ndescription: 碰 src\npaths:\n  - 'src/**'\n---\nsrc 工作流。",
    )
    return SkillStore([root], workspace=workspace)


# ---------------------------------------------------------------------------
# 激活语义
# ---------------------------------------------------------------------------


def test_conditional_hidden_until_activated(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "skills", tmp_path)
    # 无条件技能始终可见；条件技能初始不可见
    assert "always" in store.listing()
    assert "py" not in store.listing()
    assert "srcskill" not in store.listing()
    assert [s.name for s in store.available()] == ["always"]
    # 命令面同样隐藏条件技能
    assert [c["name"] for c in store.commands()] == ["skill:always"]

    store.activate_for_paths([tmp_path / "x.py"])
    assert store.is_active("py")
    assert "py" in store.listing()
    assert "srcskill" not in store.listing()
    assert sorted(s.name for s in store.available()) == ["always", "py"]
    assert "skill:py" in [c["name"] for c in store.commands()]

    # 幂等：重复激活无害；get 仍能找到所有（含未激活条件技能，供内部/测试）
    store.activate_for_paths([tmp_path / "y.py"])
    assert store.is_active("py")
    assert store.get("srcskill") is not None


def test_pattern_anchored_src_and_glob(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "skills", tmp_path)
    # src/** 命中树下任意文件（只对 src/ 前缀的文件）
    store.activate_for_paths([tmp_path / "src" / "app" / "data.txt"])
    assert store.is_active("srcskill")
    assert not store.is_active("py")  # data.txt 命中 src/**，但不是 .py —— py 未激活
    # *.py 无目录约束：命中任意目录下的 .py
    store.activate_for_paths([tmp_path / "deep" / "nested.py"])
    assert store.is_active("py")


def test_globstar_any_depth(tmp_path: Path) -> None:
    _write(
        tmp_path / "skg" / "anytests" / "SKILL.md",
        "---\ndescription: 任意测试\npaths:\n  - '**/test_*.py'\n---\n测试。",
    )
    store = SkillStore([tmp_path / "skg"], workspace=tmp_path)
    assert not store.is_active("anytests")
    store.activate_for_paths([tmp_path / "a" / "b" / "test_x.py"])
    assert store.is_active("anytests")
    # 根目录下的 test_y.py 也命中（** 允许零层）
    store2 = SkillStore([tmp_path / "skg"], workspace=tmp_path)
    store2.activate_for_paths([tmp_path / "test_y.py"])
    assert store2.is_active("anytests")


def test_activate_no_workspace_uses_absolute(tmp_path: Path) -> None:
    _write(
        tmp_path / "ska" / "abs" / "SKILL.md",
        "---\ndescription: 绝对\npaths:\n  - '**/data.json'\n---\n数据。",
    )
    store = SkillStore([tmp_path / "ska"])  # 不传 workspace：靠绝对路径匹配
    store.activate_for_paths([tmp_path / "sub" / "data.json"])
    assert store.is_active("abs")


def test_activation_survives_refresh(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "skills", tmp_path)
    store.activate_for_paths([tmp_path / "x.py"])
    store.refresh()  # 磁盘技能还在 -> 激活保留
    assert store.is_active("py")
    assert "py" in store.listing()


# ---------------------------------------------------------------------------
# fs 触碰文件触发激活（端到端）
# ---------------------------------------------------------------------------


def _fs_env(workspace: Path, skills_root: Path) -> tuple[object, SkillStore]:
    store = _make_store(skills_root, workspace)
    gate = PermissionGate(PermissionMode("auto"), workspace)
    env = ToolEnv(
        workspace=workspace,
        gate=gate,
        patches=PatchStore(),
        skills=store,
    )
    return default_registry(env), store


def test_read_file_activates_matching_skill(workspace: Path, tmp_path: Path) -> None:
    reg, store = _fs_env(workspace, tmp_path / "skills")
    assert "py" not in store.listing()
    # hello.txt 非 .py -> 不激活；但 src/app.py 命中 *.py
    reg.get("read_file").fn(path="hello.txt")
    assert not store.is_active("py")
    reg.get("read_file").fn(path="src/app.py")
    assert store.is_active("py")
    # src/app.py 同时命中 src/** 与 *.py
    assert store.is_active("srcskill")


def test_write_new_file_activates(workspace: Path, tmp_path: Path) -> None:
    reg, store = _fs_env(workspace, tmp_path / "skills")
    # 新建 tests/test_api.py 命中 ** 测试规则前先验证 *.py：write 成功即激活
    reg.get("write_file").fn(path="tools/gen.py", content="x = 1\n")
    assert store.is_active("py")
    assert "py" in store.listing()


def test_file_edit_activates(workspace: Path, tmp_path: Path) -> None:
    reg, store = _fs_env(workspace, tmp_path / "skills")
    # 先读再编辑既有文件（严格写前必读），编辑成功触发激活
    reg.get("read_file").fn(path="src/app.py")
    reg.get("file_edit").fn(path="src/app.py", old_string="def greet", new_string="def greet2")
    assert store.is_active("srcskill")


def test_skill_schema_reflects_activation_live(workspace: Path, tmp_path: Path) -> None:
    """description 是 callable：同一注册表/工具对象，激活后 schema 热更。

    这正是 agent.run 每步重算 schemas 所依赖的契约——不必重建注册表，下一步
    模型收到的工具描述里就出现了刚激活的条件技能。
    """
    reg, store = _fs_env(workspace, tmp_path / "skills")
    tool = reg.get("Skill")
    before = tool.schema()["function"]["description"]
    assert "py" not in before
    assert "always" in before
    # 触碰 src/app.py -> 激活 py（*.py）与 srcskill（src/**）
    reg.get("read_file").fn(path="src/app.py")
    after = tool.schema()["function"]["description"]
    assert "py" in after
    assert "srcskill" in after
