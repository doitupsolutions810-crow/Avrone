import os
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.provision_swarm_keys import provision


def test_provision_creates_two_distinct_private_public_key_pairs(tmp_path):
    tmp_path.chmod(0o700)
    result = provision(tmp_path)
    files = result["files"]
    assert result["private_key_material_printed"] is False
    assert set(files) == {
        "avrone-ca-requester.pem", "avrone-ca-requester.pub",
        "avrone-swarm-issuer.pem", "avrone-swarm-issuer.pub",
    }
    public_keys = []
    for name in files:
        path = tmp_path / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for prefix in ("avrone-ca-requester", "avrone-swarm-issuer"):
        private = serialization.load_pem_private_key((tmp_path / (prefix + ".pem")).read_bytes(), password=None)
        public = serialization.load_pem_public_key((tmp_path / (prefix + ".pub")).read_bytes())
        assert isinstance(private, Ed25519PrivateKey)
        assert private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ) == public.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        public_keys.append(public.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ))
    assert public_keys[0] != public_keys[1]


def test_provision_refuses_to_overwrite_existing_credentials(tmp_path):
    tmp_path.chmod(0o700)
    provision(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(FileExistsError):
        provision(tmp_path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_provision_rejects_permissive_directory(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(RuntimeError, match="mode 0700"):
        provision(tmp_path)


def test_provision_rejects_symlinked_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        provision(link)


def test_provision_cleans_partial_pair_when_atomic_link_fails(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    import scripts.provision_swarm_keys as module
    original = module.os.link
    count = [0]
    def fail_third(source, target, *, follow_symlinks=True):
        count[0] += 1
        if count[0] == 3:
            raise OSError("simulated link failure")
        return original(source, target, follow_symlinks=follow_symlinks)
    monkeypatch.setattr(module.os, "link", fail_third)
    with pytest.raises(OSError, match="simulated"):
        provision(tmp_path)
    assert list(tmp_path.iterdir()) == []
