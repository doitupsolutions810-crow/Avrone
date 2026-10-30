from avrone.tools import builtin_registry
from avrone.trinity import coerce_args


def test_flattened_shell():
    r = builtin_registry()
    assert coerce_args(r, "shell", {"tool": "shell", "cmd": "id"}, {}) == {"cmd": "id"}


def test_alias_command_and_file_path():
    r = builtin_registry()
    assert coerce_args(r, "shell", {}, {"command": "ls"}) == {"cmd": "ls"}
    assert coerce_args(r, "write_file", {}, {"file_path": "a.py", "text": "x"}) == {"path": "a.py", "content": "x"}


def test_unknown_tool_and_valid_args_untouched():
    r = builtin_registry()
    assert coerce_args(r, "nope", {}, {"command": "ls"}) == {"command": "ls"}
    assert coerce_args(r, "shell", {}, {"cmd": "ls"}) == {"cmd": "ls"}

