import pytest
from avrone.trinity import parse_action

def test_parse_action_fuzz():
    cases = [
        # malformed JSON
        ('```json\n{"tool":"shell",}\n```', None),
        ('```json\n[1,2,3]\n```', None),
        ('```json\n{not json}\n```', None),
        ('```json\n{"other":1}\n```', None),
        # multiple blocks and fenced variations
        ('prefix ```json\n{"tool":"shell"}\n``` trailing {"final":"nope"}', {'tool': 'shell'}),
        ('note {"tool": "a"} then ```json\n{"final": "b"}\n```', {'final': 'b'}),
        ('```json\n{"tool": "shell"}\n```\n```json\n{"final": "second"}\n```', {'tool': 'shell'}),
        ('```\n{"final": "plain fence"}\n```', {'final': 'plain fence'}),
        ('```JSON\n{"tool": "shell"}\n```', {'tool': 'shell'}),
        ('``` json\n{"final": "x"}\n```', {'final': 'x'}),
        # unicode and edge cases
        ('{"final": "caf\u00e9 \u2014 ok"}', {'final': 'café — ok'}),
        ('```json\n{"tool": "shell", "args": {"cmd": "echo \u00e9"}}\n```', {'tool': 'shell', 'args': {'cmd': 'echo é'}}),
        ('   {"final": "  spaced  "}  ', {'final': '  spaced  '}),
        ('{"tool": null, "final": "both"}', {'tool': None, 'final': 'both'}),
        ('{"final": {"a": 1}}', {'final': {'a': 1}}),
        ('', None),
        ('just prose', None),
        ('text {"unrelated": true} more', None),
        ('{"tool":"shell","args":{"cmd":"ls"}}', {'tool': 'shell', 'args': {'cmd': 'ls'}}),
    ]
    for raw, expected in cases:
        got = parse_action(raw)
        assert got == expected, f"parse_action({repr(raw)[:60]}...) gave {got} != {expected}"

if __name__ == "__main__":
    pytest.main([__file__])
