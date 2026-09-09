"""技能发现 / store / 渲染 单元测试。

覆盖：
- 目录式 <name>/SKILL.md、嵌套命名空间 a/b -> a:b、根级单 <x>.md；
- frontmatter 解析：description 缺省回退正文首行、user-invocable=false、
  disable-model-invocation、allowed-tools/arguments 列表与单行两种写法；
- 渲染：base-dir 头、$ARGUMENTS/${ARGUMENTS}/${CLAUDE_SKILL_DIR} 插值、
  具名参数按空格切词插值、缺词填空；
- 发现：坏文件/空正文跳过、同真实文件只去重一次、同名多文件根优先级；
- resolve_skill_roots：EMBERPY_SKILLS_DIR 覆盖、EMBER_HOME 当 home（隔离）、
  workspace 在 home 之外只扫 workspace 自身不扫真实 home；
- default_registry：env.skills 空/None 不加 Skill 工具，非空才加。
"""
from __future__ import annotations

from pathlib import Path

from emberpy.skills import SkillStore, resolve_skill_roots
from emberpy.tools import ToolEnv, default_registry
from emberpy.tools.skill_tool import build_skill_tool


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------


def test_discovers_dir_nested_and_single_file(tmp_path: Path) -> None:
    _write(
        tmp_path / "greet" / "SKILL.md",
        "---\nname: greet\ndescription: 打招呼\n---\n你好，欢迎。",
    )
    _write(
        tmp_path / "review" / "lint" / "SKILL.md",
        "---\nname: review:lint\n---\n检查代码风格。",
    )
    _write(tmp_path / "note.md", "---\n---\n记一条便签。")
    _write(tmp_path / "ignored.txt", "不是技能")

    store = SkillStore([tmp_path])
    names = {s.name for s in store.all()}
    assert {"greet", "review:lint", "note"} <= names
    assert all(s.path.name == "SKILL.md" or s.path.name == "note.md" for s in store.all())
    greet = store.get("greet")
    assert greet is not None and greet.description == "打招呼"
    assert store.get("review:lint") is not None
    # 容错：不带命名空间且唯一时也认
    assert store.get("lint") is not None


def test_bad_files_skipped_do_not_kill_load(tmp_path: Path) -> None:
    _write(tmp_path / "ok" / "SKILL.md", "---\ndescription: 好的\n---\n正文。")
    _write(tmp_path / "empty" / "SKILL.md", "---\n---\n")  # 无正文：跳过
    _write(tmp_path / "broken" / "SKILL.md", "\x00\xff no frontmatter\n正文也行")  # 容忍坏字节
    store = SkillStore([tmp_path])
    names = {s.name for s in store.all()}
    assert "ok" in names
    # 无 frontmatter -> 全文当正文，仍可加载（坏字节经 errors=replace 被替换）
    broken = store.get("broken")
    assert broken is not None and "正文也行" in broken.body


def test_duplicate_real_file_and_priority(tmp_path: Path) -> None:
    # 同名不同文件：第一个根优先
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
    _write(r1 / "dup" / "SKILL.md", "---\nname: dup\ndescription: 来自 r1\n---\n一。")
    _write(r2 / "dup" / "SKILL.md", "---\nname: dup\ndescription: 来自 r2\n---\n二。")
    store = SkillStore([r1, r2])
    assert store.get("dup") is not None
    assert store.get("dup").path.parent == r1 / "dup"

    # 同一根出现两次：同真实文件只加载一次
    once = SkillStore([r1, r1])
    assert sum(1 for s in once.all() if s.name == "dup") == 1


def test_user_invocable_false_hidden_from_commands_but_present(tmp_path: Path) -> None:
    _write(
        tmp_path / "hidden" / "SKILL.md",
        "---\nname: hidden\nuser-invocable: false\n---\n给模型内部用的。",
    )
    _write(tmp_path / "open" / "SKILL.md", "---\nname: open\n---\n公开。")
    store = SkillStore([tmp_path])
    assert store.get("hidden") is not None
    assert store.get("hidden").hidden is True
    names = [c["name"] for c in store.commands()]
    assert "skill:open" in names
    assert "skill:hidden" not in names


def test_frontmatter_lists_and_flags(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "multi" / "SKILL.md",
        "---\n"
        "name: multi\n"
        "description: 多字段\n"
        "allowed-tools: Read(run), Bash(run)\n"
        "arguments: topic depth\n"
        "when_to_use: 想测试时\n"
        "version: '1.2'\n"
        "disable-model-invocation: true\n"
        "---\n正文。",
    )
    (skills,) = SkillStore([tmp_path]).all()
    assert skills.name == "multi"
    assert skills.allowed_tools == ("Read(run)", "Bash(run)")
    assert skills.argument_names == ("topic", "depth")
    assert skills.when_to_use == "想测试时"
    assert skills.version == "1.2"
    assert skills.disable_model_invocation is True


def test_description_falls_back_to_body_first_line(tmp_path: Path) -> None:
    _write(tmp_path / "raw" / "SKILL.md", "---\nname: raw\n---\n直接干这件事。\n\n然后那样。")
    (skills,) = SkillStore([tmp_path]).all()
    assert skills.description == "直接干这件事。"


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_render_substitutes_arguments_and_dir(tmp_path: Path) -> None:
    _write(
        tmp_path / "sum" / "SKILL.md",
        "---\nname: sum\ndescription: 总结\n---\n"
        "把 ${ARGUMENTS} 总结成三点。脚本在 $CLAUDE_SKILL_DIR。",
    )
    (skill,) = SkillStore([tmp_path]).all()
    text = SkillStore([tmp_path]).render(skill, "这份文档")
    assert text.startswith("Base directory for this skill: ")
    assert str(tmp_path / "sum").replace("\\", "/") in text.splitlines()[0]
    assert "把 这份文档 总结成三点" in text
    assert "$CLAUDE_SKILL_DIR" not in text
    assert str((tmp_path / "sum").as_posix()) in text


def test_render_named_args_by_position(tmp_path: Path) -> None:
    _write(
        tmp_path / "named" / "SKILL.md",
        "---\nname: named\narguments: file label\n---\n处理 $file，标签 $label。",
    )
    (skill,) = SkillStore([tmp_path]).all()
    text = SkillStore([tmp_path]).render(skill, "src/a.py 第一版")
    assert "处理 src/a.py，标签 第一版" in text
    # 词不够时填空字符串，不报错
    text2 = SkillStore([tmp_path]).render(skill, "只给一个")
    assert "处理 只给一个，标签 " in text2


def test_render_no_arguments_clears_placeholder(tmp_path: Path) -> None:
    _write(tmp_path / "plain" / "SKILL.md", "---\nname: plain\n---\n任务：$ARGUMENTS，开始。")
    (skill,) = SkillStore([tmp_path]).all()
    assert "$ARGUMENTS" not in SkillStore([tmp_path]).render(skill, "")


# ---------------------------------------------------------------------------
# resolve_skill_roots
# ---------------------------------------------------------------------------


def test_resolve_override_env(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "my-skills"
    target.mkdir()
    monkeypatch.setenv("EMBERPY_SKILLS_DIR", str(target))
    roots = resolve_skill_roots(tmp_path / "proj")
    assert roots == [target.resolve()]
    # override 指向不存在的目录 -> 空
    monkeypatch.setenv("EMBERPY_SKILLS_DIR", str(tmp_path / "nope"))
    assert resolve_skill_roots(tmp_path / "proj") == []


def test_resolve_project_and_user_isolated(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    ws = tmp_path / "proj"
    (ws / ".agents" / "skills").mkdir(parents=True)
    _write(ws / ".agents" / "skills" / "p" / "SKILL.md", "---\n---\n项目技能。")
    user_root = home / ".agents" / "skills"
    user_root.mkdir(parents=True)
    _write(user_root / "u" / "SKILL.md", "---\n---\n用户技能。")
    monkeypatch.setenv("EMBER_HOME", str(home))
    monkeypatch.delenv("EMBERPY_SKILLS_DIR", raising=False)

    store = SkillStore(resolve_skill_roots(ws))
    names = sorted(s.name for s in store.all())
    assert names == ["p", "u"]
    assert store.get("p").source == "project"
    assert store.get("u").source == "user"


# ---------------------------------------------------------------------------
# 工具挂载与行为
# ---------------------------------------------------------------------------


def test_skill_tool_gated_on_nonempty_store(tmp_path: Path) -> None:
    no_skills = ToolEnv(workspace=tmp_path, gate=None, patches=None, skills=SkillStore([]))
    assert "Skill" not in default_registry(no_skills).names()

    plain = ToolEnv(workspace=tmp_path, gate=None, patches=None)
    assert "Skill" not in default_registry(plain).names()

    _write(tmp_path / "s" / "SKILL.md", "---\n---\n正文。")
    with_skills = ToolEnv(workspace=tmp_path, gate=None, patches=None, skills=SkillStore([tmp_path]))
    assert "Skill" in default_registry(with_skills).names()


def test_skill_tool_missing_and_disabled(tmp_path: Path) -> None:
    _write(
        tmp_path / "internal" / "SKILL.md",
        "---\nname: internal\ndisable-model-invocation: true\n---\n别调我。",
    )
    store = SkillStore([tmp_path])
    env = ToolEnv(workspace=tmp_path, gate=None, patches=None, skills=store)
    tool = build_skill_tool(env)
    # 未知技能 -> 提示可用列表
    out = tool.fn(skill="nope")
    assert "没有技能" in out and "internal" in out
    # disable-model-invocation -> 拒绝
    out2 = tool.fn(skill="internal")
    assert "disable-model-invocation" in out2
    # 正常 -> 返回已加载 + 正文
    _write(tmp_path / "go" / "SKILL.md", "---\nname: go\n---\n按此执行 $ARGUMENTS。")
    store2 = SkillStore([tmp_path])
    env2 = ToolEnv(workspace=tmp_path, gate=None, patches=None, skills=store2)
    out3 = build_skill_tool(env2).fn(skill="go", args="步骤")
    assert "已加载技能「go」" in out3
    assert "按此执行 步骤" in out3
