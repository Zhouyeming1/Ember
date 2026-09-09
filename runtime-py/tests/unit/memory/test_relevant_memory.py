"""记忆两级相关召回测试：索引常驻 + task 驱动挑全文。

场景：
① 条目 ≤ 阈值（如 3 条）给 task：仍只有索引，不注入"最相关记忆"段；
② 条目很多（61 条）但 task 为空：不触发（task 空回退纯索引）；
③ 条目很多 + 相关 task：注入 top 相关记忆全文段——描述与 task 强重叠的那条一定
   在、无关的不在、按重叠度靠前的排前面；
④ 命中的记忆正文过长会被截断（cap ~2000 字符）；
⑤ 全都没有命中的 task：不注入空段（宁缺毋滥）。
"""
from __future__ import annotations

from pathlib import Path

from emberpy.memory import MemoryStore, build_memory_prompt

REL_HEADING = "## 与本任务最相关的记忆"


def _store(root: Path) -> MemoryStore:
    return MemoryStore(root)


def _fill(store: MemoryStore, n: int, seed: str) -> None:
    """写 n 条内容与 seed 无关的填充记忆（name 形如 fill_000..）。"""
    for i in range(n):
        store.save(
            f"fill_{i:03d}",
            f"填充条目 {i}：与当前任务无关的部署运维记录 {seed}{i}",
            "reference",
            f"这是第 {i} 条用于撑规模的无关记忆。",
        )


def test_small_index_stays_pure_index_with_task(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    store.save("only", "唯一的记忆 登录", "user", "只此一条")
    text = build_memory_prompt(store, task="修复登录问题")
    assert REL_HEADING not in text
    assert "唯一的记忆" in text  # 索引在，两级召回不触发


def test_many_but_no_task_stays_pure_index(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    _fill(store, 61, "filler")
    text = build_memory_prompt(store)  # 不给 task
    assert REL_HEADING not in text


def test_relevant_reads_injected_when_many_and_task(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    # 60 条无关填充 + 2 条候选：强匹配（描述里带 登录 修复）与弱匹配（只带 登录）
    _fill(store, 60, "filler")
    store.save("strong_login", "用户偏好登录后直接进工作台的反馈与修复记录", "feedback",
               "强匹配记忆全文：用户要求登录后直接进工作台，别停在欢迎页。")
    store.save("weak_login", "某次发布顺带改了登录页文案", "reference",
               "弱匹配记忆全文：只是改了登录页的引导文案。")
    text = build_memory_prompt(store, task="修复登录后跳转问题")

    assert REL_HEADING in text
    # 强匹配在、弱匹配也可能在（登录 双字组命中）；无关的 fill 全文不进提示
    assert "用户偏好登录后直接进工作台" in text
    assert "强匹配记忆全文" in text
    # fill 记忆只出现在索引行（- [fill_…]），其正文不该被整段注入
    assert "用于撑规模的无关记忆" not in text


def test_relevant_reads_ordered_by_overlap(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    _fill(store, 60, "filler")
    # 三条都与 task 有重叠但强弱不同，倒序插入（save 顺序 = mtime 顺序）
    store.save("weak_auth", "记一下认证模块的架构", "project", "auth 架构笔记。")
    store.save("strong_pw", "密码找回功能要支持邮箱验证码", "project", "密码找回的需求细节。")
    store.save("mid_pw", "用户改密码后强制重新登录", "feedback", "改密后的登录会话处理。")
    text = build_memory_prompt(store, task="实现密码找回 密码重置")

    section = text.split(REL_HEADING, 1)[1]
    # 命中 >1 条；最高重叠（pw 双字组/词最多）那条排在最前
    assert section.index("密码找回功能要支持邮箱验证码") < section.index("用户改密码后强制重新登录")
    assert "weak_auth" not in section  # 与 task 无重叠词/双字组 -> 不注入


def test_relevant_body_truncated_when_huge(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    _fill(store, 60, "filler")
    huge = "相关正文。" * 2000  # ~8000 字符，远超 2000 cap
    store.save("big_rel", "登录 相关的超长记录", "reference", huge)
    text = build_memory_prompt(store, task="登录 的问题排查")
    assert "已截断" in text
    assert "相关正文。" * 2000 not in text


def test_no_overlap_injects_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path / "mem")
    _fill(store, 61, "filler")
    text = build_memory_prompt(store, task="量子计算论文综述")
    assert REL_HEADING not in text
