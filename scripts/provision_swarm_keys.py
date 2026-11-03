#!/usr/bin/env python3
"""Create separate AVRONE workload-assertion and Swarm-envelope Ed25519 keys."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _write_new_private_file(path: Path, data: bytes) -> None:
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    created = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # link() is atomic and refuses to overwrite an existing credential.
        os.link(temp, path, follow_symlinks=False)
        created = True
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def provision(directory: str | os.PathLike[str]) -> dict:
    target = Path(directory).expanduser()
    if target.is_symlink():
        raise RuntimeError("credential directory must not be a symlink")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = target.stat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("credential directory must have mode 0700 or stricter")

    names = (
        "avrone-ca-requester.pem",
        "avrone-ca-requester.pub",
        "avrone-swarm-issuer.pem",
        "avrone-swarm-issuer.pub",
    )
    if any((target / name).exists() or (target / name).is_symlink() for name in names):
        raise FileExistsError("one or more AVRONE trust files already exist; refusing to overwrite")

    generated = []
    try:
        for prefix in ("avrone-ca-requester", "avrone-swarm-issuer"):
            private = Ed25519PrivateKey.generate()
            public = private.public_key()
            private_pem = private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            public_pem = public.public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            raw_public = public.public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
            private_path = target / (prefix + ".pem")
            public_path = target / (prefix + ".pub")
            _write_new_private_file(private_path, private_pem)
            generated.append(private_path)
            _write_new_private_file(public_path, public_pem)
            generated.append(public_path)
            print_fingerprint = hashlib.sha256(raw_public).hexdigest()
            if prefix == "avrone-ca-requester":
                ca_fingerprint = print_fingerprint
            else:
                swarm_fingerprint = print_fingerprint
    except BaseException:
        for path in generated:
            try:
                path.unlink()
            except OSError:
                pass
        raise

    return {
        "directory": str(target.resolve()),
        "files": {name: str(target / name) for name in names},
        "sha256_fingerprints": {
            "avrone-ca-requester.pub": ca_fingerprint,
            "avrone-swarm-issuer.pub": swarm_fingerprint,
        },
        "private_key_material_printed": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="./secrets")
    args = parser.parse_args(argv)
    print(json.dumps(provision(args.directory), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
