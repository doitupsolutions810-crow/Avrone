import pytest
from pathlib import Path

def test_skills_r4_frontmatter():
    root = Path(__file__).resolve().parents[1]
    skills = [
        ('self-evolve', 'safely improves its own code via evolve_propose'),
        ('edge-tooling', 'Playbook for building new bounded agentic tools')
    ]
    for name, desc_snippet in skills:
        path = root / 'skills' / name / 'SKILL.md'
        assert path.exists(), f'{path} missing'
        content = path.read_text(encoding='utf-8')
        assert 'name:' in content and name in content, f'Missing name: {name}'
        assert 'description:' in content and desc_snippet in content, f'Missing description or snippet for {name}'