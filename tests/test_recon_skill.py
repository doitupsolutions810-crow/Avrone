import unittest
import re

def get_frontmatter(path):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    match = re.search(r'^---\s*(.*?)\s*---', content, re.DOTALL | re.MULTILINE)
    if not match:
        return {}
    fm_text = match.group(1).strip()
    fm = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if ':' in line:
            key, value = [x.strip() for x in line.split(':', 1)]
            value = value.strip("'").strip('"')
            fm[key] = value
    return fm

class TestReconSkill(unittest.TestCase):
    def test_loads_with_frontmatter(self):
        fm = get_frontmatter('skills/recon-advanced/SKILL.md')
        self.assertEqual(fm.get('name'), 'recon-advanced')
        desc = fm.get('description', '')
        self.assertTrue(desc.startswith('Authorized network reconnaissance playbook'))

    def test_network_mapping_frontmatter(self):
        fm = get_frontmatter('skills/network-mapping/SKILL.md')
        self.assertEqual(fm.get('name'), 'network-mapping')
        self.assertIn('safely', fm.get('description', ''))

if __name__ == '__main__':
    unittest.main()
