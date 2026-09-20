# @File:     skills.py
# @Author:   mjh
# @DateTime: 2026/09/20
"""Skills：SKILL.md 渐进披露技能（发现/解析/清单/渲染）。

双目录：项目级 <工作区>/.agent/skills/（随仓库共享）+ 用户级 ~/.agent/skills/
（跨项目），同名项目级优先。清单注入 system prompt（模型自主 Read 全文），
斜杠命令 /<name> 触发渲染。零配置：目录不存在即零行为。
"""
import os
from dataclasses import dataclass, field

import yaml


@dataclass
class Skill:
    name: str                       # 触发名（/<name>）
    description: str
    path: str                       # SKILL.md 绝对路径（模型 Read 全文用）
    argument_hint: str | None = None
    allowed_tools: list[str] | None = None   # None = 不收紧；仅斜杠触发路径生效
    body: str = field(default="")   # frontmatter 之后的正文


def _split_tools(v) -> list[str] | None:
    """allowed-tools 字段：逗号串或 YAML 列表；空白视为未设置。"""
    if v is None:
        return None
    raw = [str(x).strip() for x in (v.split(",") if isinstance(v, str) else v)]
    return [x for x in raw if x] or None


def _parse_frontmatter(text: str):
    """返回 (meta: dict | None, body)。首行 --- 且有闭合 --- 才算有头。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            meta = yaml.safe_load("\n".join(lines[1:i]))
            return (meta if isinstance(meta, dict) else {}), "\n".join(lines[i + 1:])
    return None, text               # 头未闭合：当无 frontmatter 处理


def _load_one(dir_path: str, dir_name: str) -> Skill | None:
    """加载单个技能目录；任何不合格返回 None（调用方打 ⚠）。"""
    md = os.path.join(dir_path, "SKILL.md")
    try:
        with open(md, encoding="utf-8") as f:
            text = f.read()
        meta, body = _parse_frontmatter(text)
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        print(f"[agent] ⚠ 技能 {dir_name} 加载失败: {type(e).__name__}: {e}")
        return None
    desc = meta.get("description") if meta else None
    if not isinstance(desc, str) or not desc.strip():
        print(f"[agent] ⚠ 技能 {dir_name} 跳过：frontmatter 缺 description（必填）")
        return None
    name = meta.get("name") if meta else None
    if not isinstance(name, str) or not name.strip():
        name = dir_name
    hint = meta.get("argument-hint")
    return Skill(
        name=name.strip(), description=desc.strip(), path=os.path.abspath(md),
        argument_hint=hint.strip() if isinstance(hint, str) and hint.strip() else None,
        allowed_tools=_split_tools(meta.get("allowed-tools")),
        body=body)


def load_skills(workspace_root: str, user_root: str | None = None) -> list[Skill]:
    """发现全部技能：项目级优先，同名去重，按名字排序（输出稳定）。"""
    if user_root is None:
        user_root = os.path.join(os.path.expanduser("~"), ".agent", "skills")
    skills: dict[str, Skill] = {}
    for base in (os.path.join(workspace_root, ".agent", "skills"), user_root):
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            if entry in skills or not os.path.isdir(os.path.join(base, entry)):
                continue             # 同名：项目级已注册，用户级让位
            s = _load_one(os.path.join(base, entry), entry)
            if s is not None:
                skills[s.name] = s
    return [skills[k] for k in sorted(skills)]


def catalog_note(skills: list[Skill]) -> str:
    """注入 system prompt 尾部的技能清单（渐进披露：只列名字+描述+路径，模型自己 Read 全文）。"""
    if not skills:
        return ""
    lines = ["\n\n# 可用技能（任务相关时先用 Read 读 SKILL.md 全文，再按其指引行事；用户也可 /<名字> 直接触发）"]
    lines += [f"- {s.name}：{s.description}（Read {s.path}）" for s in skills]
    return "\n".join(lines)


def render(skill: Skill, args: str) -> str:
    """斜杠触发时的正文渲染：$ARGUMENTS 全部替换；无占位符有参数则附尾。"""
    if "$ARGUMENTS" in skill.body:
        return skill.body.replace("$ARGUMENTS", args)
    if not args:
        return skill.body
    return skill.body.rstrip("\n") + f"\n\n用户参数：{args}"
