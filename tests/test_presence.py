import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from avrone.presence import environment, agents, snapshot, directory, find_agent, render, flatten, _host_interfaces, _CACHE

@pytest.fixture(autouse=True)
def clear_cache():
    _CACHE.clear()
    yield
    _CACHE.clear()

@pytest.fixture
def mock_core():
    core = MagicMock()
    core.active = {}
    core.tools = MagicMock()
    core.tools.names.return_value = ["t1"]
    core.settings = MagicMock(llm_model="mock", llm_base_url=None, docker_image=None)
    core.runner = MagicMock(kind="local", workspace="/ws", container=None)
    core.extensions = {"mcp": {"servers": {"s1": {}}}}
    core.fleet = None
    return core

def test_host_interfaces(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: True)
    mock_run = MagicMock(stdout='[{"ifname":"eth0","operstate":"UP","addr_info":[{"local":"10.0.0.1","prefixlen":24}]}]')
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_run)
    ifs = _host_interfaces()
    assert len(ifs) > 0 and "eth0" in ifs[0]["name"]
    monkeypatch.setattr("shutil.which", lambda x: False)
    with patch("socket.getaddrinfo", return_value=[(2,2,0,0,("127.0.0.1",))]), patch("socket.socket") as s:
        s.return_value.getsockname.return_value = ("10.0.0.2", 0)
        ifs = _host_interfaces()
        assert any("127.0.0.1" in str(d) for d in ifs)

def test_environment(mock_core):
    core = mock_core
    e = asyncio.run(environment(core, ttl=0))
    assert "device" in e and e["runner"]["kind"] == "local" and e["services"]["tools"] == 1
    e2 = asyncio.run(environment(core, ttl=60))
    assert e2 is e
    core.runner.kind = "docker"
    core.runner.run = AsyncMock(return_value=MagicMock(stdout="hostname\n10.0.0.3"))
    core.runner.container = "c1"
    core.settings.docker_image = "i1"
    e3 = asyncio.run(environment(core, ttl=0))
    assert e3["runner"]["container"]["name"] == "c1" and "10.0.0.3" in e3["runner"]["container"]["addrs"]

def test_agents(mock_core):
    core = mock_core
    core.active = {"A1": {"status": "working", "started": 100, "role": "r1", "parent": None, "run_id": "1", "task": "t", "depth": 0}, "A2": {"status": "idle", "started": 50, "role": "r2"}}
    a = agents(core)
    assert len(a) == 2 and a[0]["name"] == "A1"

def test_snapshot(mock_core):
    core = mock_core
    s = asyncio.run(snapshot(core))
    assert s["node_id"] == "local" and "env" in s and "children" in s
    core.fleet = MagicMock()
    core.fleet.nodes = {"child": {"presence": {"node_id": "child", "agents": []}, "url": "u1"}}
    core.fleet.status_of.return_value = "online"
    s2 = asyncio.run(snapshot(core))
    assert "child" in s2["children"]
    assert s2["children"]["child"]["status"] == "online"

def test_directory_flatten(mock_core):
    core = mock_core
    with patch("avrone.presence.snapshot", new_callable=AsyncMock, return_value={"node_id": "root", "status": "online", "env": {}, "agents": [], "children": {"c1": {"node_id": "c1", "status": "online", "env": {}, "agents": [], "children": {}}}}) as p:
        d = asyncio.run(directory(core))
        assert len(d) == 2 and d[1]["route"] == "c1"

def test_find_agent():
    rows = [{"route": "", "agents": [{"name": "A1", "status": "working"}]}, {"route": "r1", "agents": [{"name": "A2", "status": "idle"}]}]
    assert find_agent(rows, "A1") == ("", "A1")
    assert find_agent(rows, "r1/A2") == ("r1", "A2")
    with pytest.raises(LookupError):
        find_agent(rows, "bad")
    rows2 = [{"route": "", "agents": [{"name": "Dup", "status": "working"}]}, {"route": "r1", "agents": [{"name": "Dup", "status": "working"}]}]
    with pytest.raises(LookupError):
        find_agent(rows2, "Dup")

def test_render():
    rows = [{"route": "", "node_id": "n1", "status": "online", "url": None, "env": {"device": {"hostname": "h1", "os": "Linux"}, "runner": {"kind": "local"}, "networks": [{"name": "lo", "addrs": ["127.0.0.1"]}], "services": {"public_url": "u", "tools": 5, "mcp_servers": ["m1"], "model": "m"}}, "agents": [{"name": "Ag1", "role": "test", "status": "working", "run_id": "r1", "task": "do it"}]}]
    out = render(rows)
    assert "(this node)" in out and "Ag1" in out and "networks" in out
    out2 = render(rows, only_live=True)
    assert "Ag1" in out2
    rows[0]["agents"][0]["status"] = "stopped"
    out3 = render(rows, only_live=True)
    assert "Ag1" not in out3
