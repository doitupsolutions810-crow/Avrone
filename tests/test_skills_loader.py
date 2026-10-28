"""Skill loader robustness: frontmatter parsing, bad files, traversal, duplicates, ordering."""
import asyncio
from types import SimpleNamespace

import pytest

from avrone import agentkit


def run(c):
    return asyncio.run(c)


@pytest.fixture
def env(tmp_path, monkeypatch):
    sk = tmp_path / "skills"
    sk.mkdir()
    monkeypatch.setenv("AVRONE_SKILLS_DIR", str(sk))
    events = []
    core = SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: events.append((a, k))))
    ctx = SimpleNamespace(core=core, run_id="r1", name="Agent-1", depth=0)
    return SimpleNamespace(dir=sk, ctx=ctx, events=events, root=tmp_path)


def skill(env, name, content, flat=False):
    if flat:
        p = env.dir / f"{name}.md"
    else:
        (env.dir / name).mkdir(exist_ok=True)
        p = env.dir / name / "SKILL.md"
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


def listing(env):
    return run(agentkit.t_skill_list(env.ctx, {}))


def load(env, name):
    return run(agentkit.t_skill_load(env.ctx, {"name": name}))


# ---------------------------------------------------------------- parse_skill
def test_parse_skill_frontmatter_and_body():
    fm, body = agentkit.parse_skill("---\nname: a\ndescription: 'Do A'\n---\n1. step\n")
    assert fm == {"name": "a", "description": "Do A"} and body.strip() == "1. step"


def test_parse_skill_missing_unterminated_and_empty():
    assert agentkit.parse_skill("1. just steps") == ({}, "1. just steps")
    fm, body = agentkit.parse_skill("---\nname: a\ndescription: x\n1. never closed")
    assert fm == {} and "never closed" in body
    assert agentkit.parse_skill("") == ({}, "")


def test_parse_skill_bom_colons_and_first_key_wins():
    fm, _ = agentkit.parse_skill("\ufeff---\nName: a\ndescription: run: then stop\ndescription: dup\njunk line\n---\n")
    assert fm == {"name": "a", "description": "run: then stop"}


# ---------------------------------------------------------------- listing
def test_missing_frontmatter_falls_back_to_first_line(env):
    skill(env, "plain", "\n\n# Plain playbook\n1. do it\n")
    assert "- plain: Plain playbook" in listing(env)


def test_malformed_frontmatter_does_not_crash(env):
    skill(env, "broken", "---\nname broken\n:::\n---\n1. step one\n")
    skill(env, "open", "---\ndescription: never closed\n1. step\n")
    out = listing(env)
    assert "- broken: 1. step one" in out
    assert "- open:" in out


def test_missing_description_uses_body_not_frontmatter(env):
    skill(env, "nodesc", "---\nname: nodesc\n---\nFirst real line\n")
    assert "- nodesc: First real line" in listing(env)


def test_description_only_read_from_frontmatter(env):
    # a "description:" line inside the steps must not override the real one
    skill(env, "x", "---\nname: x\ndescription: Real\n---\n1. set description: fake\n")
    assert "- x: Real" in listing(env)


def test_empty_file_listed_but_refused_on_load(env):
    skill(env, "empty", "")
    skill(env, "blank", "   \n\n")
    out = listing(env)
    assert "- empty: (no description)" in out and "- blank: (no description)" in out
    assert load(env, "empty").startswith("ERROR")
    assert env.events == []


def test_non_utf8_bytes(env):
    skill(env, "latin", b"---\nname: latin\ndescription: caf\xe9 \xff\xfe\n---\n1. step \x80\n")
    out = listing(env)
    assert "- latin: caf" in out
    text = load(env, "latin")
    assert "1. step" in text and "\ufffd" in text


def test_sorted_and_stable_across_kinds(env):
    skill(env, "zeta", "---\ndescription: z\n---\n")
    skill(env, "alpha", "---\ndescription: a\n---\n", flat=True)
    skill(env, "mid", "---\ndescription: m\n---\n")
    first = listing(env)
    names = [line.split(":")[0][2:] for line in first.splitlines()]
    assert names == ["alpha", "mid", "zeta"]
    assert listing(env) == first


def test_duplicate_names_directory_wins(env):
    skill(env, "dup", "---\ndescription: from dir\n---\nDIR BODY\n")
    skill(env, "dup", "---\ndescription: from flat\n---\nFLAT BODY\n", flat=True)
    out = listing(env)
    assert out.count("- dup:") == 1 and "from dir" in out
    assert "DIR BODY" in load(env, "dup")


def test_no_skills_and_missing_dir(env, monkeypatch):
    assert listing(env) == "no skills installed"
    monkeypatch.setenv("AVRONE_SKILLS_DIR", str(env.root / "does-not-exist"))
    assert listing(env) == "no skills installed"
    assert load(env, "x").startswith("ERROR: no skill")


def test_oversized_file_skipped(env, monkeypatch):
    monkeypatch.setattr(agentkit, "SKILL_MAX_BYTES", 50)
    skill(env, "big", "---\ndescription: big\n---\n" + "x" * 100)
    skill(env, "small", "---\ndescription: small\n---\n")
    assert "big" not in listing(env) and "small" in listing(env)


def test_unsafe_directory_names_skipped(env):
    skill(env, ".hidden", "---\ndescription: hidden\n---\n")
    skill(env, "bad name", "---\ndescription: spaces\n---\n")
    assert listing(env) == "no skills installed"


# ---------------------------------------------------------------- loading
def test_load_ok_emits_event(env):
    skill(env, "good", "---\nname: good\ndescription: g\n---\n1. step\n")
    assert "1. step" in load(env, "good")
    assert env.events and env.events[0][0][1] == "skill.load"


@pytest.mark.parametrize("name", ["../x", "..", "../../etc/passwd", "a/b", "/etc/passwd", "good/../good",
                                  "..\\x", "x\x00", ".hidden", ""])
def test_path_traversal_refused(env, name):
    skill(env, "good", "---\ndescription: g\n---\n1. step\n")
    (env.root / "x.md").write_text("---\ndescription: outside\n---\nSECRET\n")
    (env.root / "x").mkdir()
    (env.root / "x" / "SKILL.md").write_text("SECRET\n")
    out = load(env, name)
    assert out.startswith("ERROR") and "SECRET" not in out
    assert env.events == []


def test_symlink_escaping_skills_dir_skipped(env):
    outside = env.root / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\ndescription: escaped\n---\nSECRET\n")
    try:
        (env.dir / "evil").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unsupported")
    assert "evil" not in listing(env)
    assert "SECRET" not in load(env, "evil")


def test_missing_name_arg(env):
    assert run(agentkit.t_skill_load(env.ctx, {})).startswith("ERROR")


def test_load_truncates(env, monkeypatch):
    monkeypatch.setattr(agentkit, "SKILL_LOAD_CHARS", 10)
    skill(env, "long", "---\ndescription: l\n---\n" + "y" * 100)
    assert len(load(env, "long")) == 10
