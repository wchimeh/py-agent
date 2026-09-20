# @File:     test_skills.py
# @Author:   mjh
# @DateTime: 2026/09/20
"""Skills 发现与解析单测：tmp_path 造假目录树，零网络零全局状态。"""
import os

from agent.skills import Skill, catalog_note, load_skills, render


def _mk(skills_dir, name, frontmatter="", body="正文内容", fname="SKILL.md"):
    """在技能目录（=含 <name> 子目录的那层）下造 <name>/<fname>；frontmatter 为 None 表示不写头。"""
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    text = body if frontmatter is None else f"---\n{frontmatter}\n---\n{body}"
    d.joinpath(fname).write_text(text, encoding="utf-8")
    return d.joinpath(fname)


def _proj(tmp_path):
    return tmp_path / ".agent" / "skills"


def test_load_discovers_project_level(tmp_path):
    user = tmp_path / "home"          # 空用户级目录：只验证项目级
    _mk(_proj(tmp_path), "commit", frontmatter=(
        "name: commit\n"
        "description: 提交前检查测试与 lint\n"
        "argument-hint: \"[消息]\"\n"
        "allowed-tools: Bash, Read"))
    skills = load_skills(str(tmp_path), user_root=str(user))
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "commit"
    assert s.description == "提交前检查测试与 lint"
    assert s.argument_hint == "[消息]"
    assert s.allowed_tools == ["Bash", "Read"]
    assert s.body == "正文内容"
    assert s.path == str(tmp_path / ".agent" / "skills" / "commit" / "SKILL.md")


def test_load_discovers_user_level(tmp_path):
    user = tmp_path / "home"
    _mk(user, "deploy", frontmatter="description: 部署流程")
    skills = load_skills(str(tmp_path), user_root=str(user))
    assert [s.name for s in skills] == ["deploy"]
    assert "home" in skills[0].path


def test_project_overrides_user_same_name(tmp_path):
    user = tmp_path / "home"
    _mk(_proj(tmp_path), "lint", frontmatter="description: 项目级 lint")
    _mk(user, "lint", frontmatter="description: 用户级 lint")
    skills = load_skills(str(tmp_path), user_root=str(user))
    assert len(skills) == 1
    assert skills[0].description == "项目级 lint"
    assert str(tmp_path) in skills[0].path


def test_name_defaults_to_dir_name(tmp_path):
    _mk(_proj(tmp_path), "wordcount", frontmatter="description: 统计")
    skills = load_skills(str(tmp_path), user_root=str(tmp_path / "none"))
    assert skills[0].name == "wordcount"


def test_missing_description_skipped(tmp_path):
    _mk(_proj(tmp_path), "nodesc", frontmatter="name: nodesc")
    skills = load_skills(str(tmp_path), user_root=str(tmp_path / "none"))
    assert skills == []


def test_no_frontmatter_skipped(tmp_path):
    _mk(_proj(tmp_path), "bare", frontmatter=None, body="没有头的文档")
    skills = load_skills(str(tmp_path), user_root=str(tmp_path / "none"))
    assert skills == []


def test_bad_yaml_skipped(tmp_path):
    d = tmp_path / ".agent" / "skills" / "bad"
    d.mkdir(parents=True)
    d.joinpath("SKILL.md").write_text(
        "---\nname: [unclosed\n  bad indent\n---\n正文", encoding="utf-8")
    skills = load_skills(str(tmp_path), user_root=str(tmp_path / "none"))
    assert skills == []


def test_allowed_tools_forms(tmp_path):
    _mk(_proj(tmp_path), "listform", frontmatter="description: d\nallowed-tools:\n  - Read\n  - Glob")
    _mk(_proj(tmp_path), "blank", frontmatter='description: d\nallowed-tools: ""')
    skills = load_skills(str(tmp_path), user_root=str(tmp_path / "none"))
    by = {s.name: s for s in skills}
    assert by["listform"].allowed_tools == ["Read", "Glob"]
    assert by["blank"].allowed_tools is None      # 空白视为未设置（不收紧）


def test_dirs_missing_returns_empty(tmp_path):
    assert load_skills(str(tmp_path), user_root=str(tmp_path / "nope")) == []


def test_only_skill_md_counts_sibling_files_ignored(tmp_path):
    # 技能目录里的辅助文件（脚本/参考文档）不参与发现，但保留在原处供 Read
    _mk(_proj(tmp_path), "helper", frontmatter="description: d")
    helper_dir = tmp_path / ".agent" / "skills" / "helper"
    helper_dir.joinpath("refs.py").write_text("x = 1", encoding="utf-8")
    skills = load_skills(str(tmp_path), user_root=str(tmp_path / "none"))
    assert [s.name for s in skills] == ["helper"]
    assert os.path.exists(helper_dir / "refs.py")


def test_unknown_frontmatter_keys_ignored(tmp_path):
    _mk(_proj(tmp_path), "extra", frontmatter="description: d\nlicense: MIT\nversion: 2")
    skills = load_skills(str(tmp_path), user_root=str(tmp_path / "none"))
    assert len(skills) == 1                        # 未知键宽容不报错


def _skill(**kw):
    base = dict(name="wc", description="统计", path="/x/SKILL.md",
                argument_hint=None, allowed_tools=None, body="正文")
    base.update(kw)
    return Skill(**base)


# ---------- render：$ARGUMENTS / 附尾 ----------

def test_render_replaces_all_placeholders():
    s = _skill(body="目标：$ARGUMENTS\n再念一遍：$ARGUMENTS")
    assert render(s, "a.txt") == "目标：a.txt\n再念一遍：a.txt"

def test_render_appends_args_when_no_placeholder():
    s = _skill(body="按流程办事")
    assert render(s, "extra input") == "按流程办事\n\n用户参数：extra input"

def test_render_empty_args_with_placeholder_becomes_blank():
    s = _skill(body="目标：$ARGUMENTS。")
    assert render(s, "") == "目标：。"

def test_render_no_args_no_placeholder_unchanged():
    s = _skill(body="按流程办事")
    assert render(s, "") == "按流程办事"


# ---------- catalog_note：system 注入的清单文本 ----------

def test_catalog_note_lists_name_description_path():
    s1 = _skill(name="commit", description="提交前检查",
                path="G:/w/.agent/skills/commit/SKILL.md")
    s2 = _skill(name="deploy", description="部署流程",
                path="C:/Users/u/.agent/skills/deploy/SKILL.md")
    note = catalog_note([s1, s2])
    assert "commit" in note and "提交前检查" in note
    assert "deploy" in note and "部署流程" in note
    assert "G:/w/.agent/skills/commit/SKILL.md" in note   # 模型 Read 的路径
    assert "C:/Users/u/.agent/skills/deploy/SKILL.md" in note
    assert "Read" in note                                  # 渐进披露指引

def test_catalog_note_empty_is_blank():
    assert catalog_note([]) == ""
