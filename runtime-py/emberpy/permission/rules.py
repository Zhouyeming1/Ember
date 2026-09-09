"""命令分类规则：硬拦截 / 危险 / 只读白名单 -> CommandKind。

对应 Ember README 里"沙箱是纵深防御，不是审命令的替代"：
这里的模式匹配只是启发式防线，真正的边界由使用方（前端/沙箱）配合，
不能把它当作完整的安全边界。

R-D（二轮对照）补的维度：claude 靠"写路径安全 + 只读放行判定 + 重定向目标越界"
三层，引擎原先只有字符串正则——漏掉 ``echo x > /etc/…``（echo 在白名单当只读）、
``rm /etc/passwd``（无 -rf 当 safe）、``$IFS``/命令替换变量绕掉全部 ``\\b``+空格
规则。这里给 classify_command 加可选的 workspace 感知：重定向目标、rm/chmod/
sed 等位置参数落到"系统/敏感/工作区外"目标时升 DANGEROUS（auto 询问、plan 拒绝）；
echo 带输出重定向不再算只读。parser 差分 / per-subcommand 规则引擎明确不做（可裁）。
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Optional

from .modes import CommandKind

# 无论什么权限模式都拒绝的命令（纵深防御，full 也不例外）。
# 注意：这只是字符串启发式，不是真正的文件系统隔离。
_HARD_BLOCK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+(-[a-z]*[rf][a-z]*\s*)?\.[ \t]+[^ \t]"),  # rm -rf . xxx
    re.compile(r"\bdd\b[^|;]*\bof=(?:/dev/(?:sd|hd|nvme))", re.I),  # dd 写块设备
    re.compile(r"\s(?:>\s*|>>\s*)?/dev/(?:sd|hd|nvme)", re.I),  # 重定向写块设备
    re.compile(r":\(\)\s*\{\s*:\|\s*:&\s*\}\s*;"),  # fork bomb
    re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b.*\b/dev/(?:sd|hd|nvme)", re.I),  # 直接格式化磁盘
]

# 命令里以空格分隔的 token
_TOKEN_SPLIT = re.compile(r"\S+")


def _deletes_filesystem_root(command: str) -> bool:
    """rm 的参数里带着裸根目录 / 或 ~（删除根/家目录），无论什么模式都拦。"""
    if not re.search(r"\brm\b", command):
        return False
    return any(token in ("/", "~", "~\\") for token in _TOKEN_SPLIT.findall(command))


# 只读诊断命令：plan 模式下只允许这些（工作区内执行）。
# 白名单刻意保持很小——像 cat 这类能读任意主机路径的命令不放进白名单，
# 避免 agent 在 plan 模式下通过 shell 把敏感文件内容带进上下文。
_READ_ONLY_COMMANDS: list[re.Pattern[str]] = [
    re.compile(r"^(?:pwd|ls|dir)$"),
    re.compile(r"^(?:git|hg)\s+(?:status|log|diff|branch|stash\s+list)\b"),
    re.compile(r"^(?:echo)\b"),
]

# 有破坏性的命令：auto 模式下默认询问（full 放行）。
_DANGEROUS_COMMANDS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-[a-z]*[rf][a-z]*", re.I),  # rm -r/-f（根目录由上面硬拦截兜底）
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+clean\s+-[a-z]*[fd]", re.I),
    re.compile(r"\bgit\s+checkout\s+--\s*\.", re.I),
    re.compile(r"\bgit\s+push\s+(?:-[a-z]*f|-f[a-z]*|--force)", re.I),
    re.compile(r"\bchmod\s+-R\s+777\b"),
    re.compile(r"\bshutdown\b|\breboot\b|\binit\s+0\b|\bpoweroff\b"),
    re.compile(r"\bcurl\b.*\|\s*(?:ba)?sh\b|\bwget\b.*\|\s*(?:ba)?sh\b"),  # 远程管道执行
    re.compile(r"\bsudo\b"),
    re.compile(r"\$\{?\s*IFS\s*\}?"),  # $IFS / ${IFS}：把空格隐藏进变量，绕字符串正则
]

# ---------------------------------------------------------------------------
# R-D：目标路径扫描（shlex 拆 argv，不做 shell 语法差分）
# ---------------------------------------------------------------------------

# 目标命中即升 DANGEROUS 的系统目录前缀（Unix；Windows 敏感靠段名兜底）
_SYSTEM_PREFIXES = (
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/var",
    "/proc", "/dev", "/sys", "/lib", "/root", "/home",
)
# 路径任一段命中即危险的目录名（与 policy 的敏感集合口径一致）
_SENSITIVE_DIR_SEGS = {".git", ".ssh", ".aws", ".gnupg", ".claude", ".ember", ".config"}
# 文件名命中即危险（shell 侧写配置文件）
_SENSITIVE_BASENAMES = {
    ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile",
    ".gitconfig", ".gitmodules", ".mcp.json", ".claude.json", ".env",
}
# 管道/命令分隔符（切"简单命令"用）
_CMD_SEPS = (";", "&&", "||", "|", "&")
# 位置参数里有"写目标"的命令（扫描它们的目标）
_TARGET_CMDS = {
    "rm", "rmdir", "chmod", "chown", "sed", "cp", "mv",
    "mkdir", "touch", "dd", "curl", "wget", "tar",
}
# 重定向目标正则：捕获 ``>``/``>>`` 后到空白/分隔符之间的 token
_REDIR_RE = re.compile(r">\s*(?P<target>[^ \t;&|<>]+)")


def _redirection_targets(command: str) -> list[str]:
    """抽出命令里所有重定向目标（``2>&1`` 的 ``&1`` 与空 token 忽略）。"""
    out: list[str] = []
    for m in _REDIR_RE.finditer(command):
        t = m.group("target")
        if not t or t.startswith("&"):
            continue
        out.append(t.strip("\"'"))
    return out


def _expand_target(token: str) -> Path:
    """展开 ~ / ~/… / $HOME 前缀；其余原样（相对路径相对 shell cwd = workspace）。"""
    t = token.strip().strip("\"'")
    if t in ("~", "~/"):
        return Path.home()
    if t.startswith("~/"):
        return Path.home() / t[2:]
    if t.startswith("$HOME"):
        return Path(t.replace("$HOME", str(Path.home()), 1))
    return Path(t)


def _system_prefix(p: Path) -> bool:
    s = str(p).replace("\\", "/").lower()
    for pre in _SYSTEM_PREFIXES:
        if s == pre or s.startswith(pre + "/"):
            return True
    return False


def _sensitive_seg(p: Path) -> bool:
    parts = [seg.lower() for seg in p.parts if seg not in ("", "/", "\\")]
    if parts and parts[-1] in _SENSITIVE_BASENAMES:
        return True
    return any(seg in _SENSITIVE_DIR_SEGS for seg in parts)


def _bad_target(token: str, workspace: Optional[Path]) -> bool:
    """单个目标是否"写/删到受保护或越界路径"（True = 该升 DANGEROUS）。"""
    p = _expand_target(token)
    if _system_prefix(p) or _sensitive_seg(p):
        return True
    # 相对工作区判越界：shell cwd = workspace，故相对目标以 workspace 为根
    cand = p if p.is_absolute() else Path(workspace or Path.cwd()) / p
    if workspace is not None and not _is_within(cand, Path(workspace)):
        return True
    return False


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _strip_flags(seg: list[str]) -> list[str]:
    """去掉前导 flag（以 - 开头）；-- 之后的不再当 flag。"""
    out: list[str] = []
    end_flags = False
    for tok in seg:
        if not end_flags and tok == "--":
            end_flags = True
            continue
        if not end_flags and tok.startswith("-"):
            continue
        out.append(tok)
    return out


def _seg_dangerous(cmd: str, seg: list[str], workspace: Optional[Path]) -> bool:
    """一条简单命令的 argv 里是否有写/删目标落到受保护/越界路径。"""
    if not seg:
        return False
    if cmd in ("rm", "rmdir"):
        return any(_bad_target(t, workspace) for t in _strip_flags(seg))
    if cmd in ("chmod", "chown"):
        pos = _strip_flags(seg)
        if not pos:
            return False
        # 第一个非 flag 参数是模式，其余是目标（chmod 777 file）
        return any(_bad_target(t, workspace) for t in pos[1:])
    if cmd == "sed":
        # 只有就地编辑（-i/--in-place）才写文件：目标是最后一个非 flag 参数
        if not any(tok == "-i" or tok.startswith("--in-place") or (tok.startswith("-i") and not tok.startswith("-in")) for tok in seg):
            return False
        pos = _strip_flags(seg)
        if pos and _bad_target(pos[-1], workspace):
            return True
        return False
    if cmd in ("cp", "mv"):
        pos = _strip_flags(seg)
        if len(pos) >= 2 and _bad_target(pos[-1], workspace):
            return True
        return False
    if cmd in ("mkdir", "touch"):
        return any(_bad_target(t, workspace) for t in _strip_flags(seg))
    if cmd == "dd":
        return any(tok.startswith("of=") and _bad_target(tok[3:], workspace) for tok in seg)
    if cmd in ("curl", "wget"):
        for idx, tok in enumerate(seg[:-1]):
            if tok in ("-o", "--output"):
                return _bad_target(seg[idx + 1], workspace)
        return False
    if cmd == "tar":
        for idx, tok in enumerate(seg[:-1]):
            if tok in ("-C", "--directory"):
                return _bad_target(seg[idx + 1], workspace)
        return False
    return False


def _dangerous_target(command: str, workspace: Optional[Path]) -> bool:
    """目标扫描：重定向 + 常见写/删命令的位置参数命中受保护/越界目标。"""
    for t in _redirection_targets(command):
        if t not in ("/dev/null", "nul") and _bad_target(t, workspace):
            return True
    try:
        toks = shlex.split(command)
    except ValueError:  # 引号没闭合等：退回空格切，目标扫描降级
        toks = _TOKEN_SPLIT.findall(command)
    i = 0
    while i < len(toks):
        low = toks[i].lower()
        starts_cmd = i == 0 or toks[i - 1] in _CMD_SEPS
        if low in _TARGET_CMDS and starts_cmd:
            j = i + 1
            while j < len(toks) and toks[j] not in _CMD_SEPS:
                j += 1
            if _seg_dangerous(low, toks[i + 1:j], workspace):
                return True
            i = j
        else:
            i += 1
    return False


def _has_output_redirection(command: str) -> bool:
    """命令是否带输出重定向（排除 /dev/null 这类无害 sink）。"""
    for t in _redirection_targets(command):
        if t not in ("/dev/null", "nul"):
            return True
    return False


def _match_any(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def classify_command(command: str, workspace: Optional[Path] = None) -> CommandKind:
    """把一条 shell 命令粗分为四类，供不同权限模式裁决。

    workspace 可选：给定时多做一层"目标路径"判定（重定向/rm/chmod/sed 的目标落到
    受保护/工作区外 -> DANGEROUS），不给则退化为纯字符串启发式（兼容旧调用/测试）。
    """
    if _deletes_filesystem_root(command):
        return CommandKind.BLOCKED
    if _match_any(_HARD_BLOCK_PATTERNS, command):
        return CommandKind.BLOCKED
    # 目标扫描先于正则危险表：echo > /etc/x 之类让字符串正则漏掉的在这兜住
    if _dangerous_target(command, workspace):
        return CommandKind.DANGEROUS
    if _match_any(_DANGEROUS_COMMANDS, command):
        return CommandKind.DANGEROUS
    if _match_any(_READ_ONLY_COMMANDS, command):
        # 白名单命令带输出重定向（写文件/写外部）就不再是纯只读（对齐 claude
        # 只读判定：有重定向即不算只读，plan 不能放行、auto 当普通命令）
        if _has_output_redirection(command):
            return CommandKind.SAFE
        return CommandKind.READ_ONLY
    return CommandKind.SAFE
