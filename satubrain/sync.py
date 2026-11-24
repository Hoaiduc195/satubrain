"""Opt-in encrypted snapshots that travel through a user-selected folder."""

import json
import os
from pathlib import Path
import re
import tempfile

from .store import MAX_SNAPSHOT, json_bytes, validate_snapshot

MAGIC = b"SATUBRAIN\x01"
HEADER_SIZE = len(MAGIC) + 32 + 16 + 12
MAX_ENVELOPE = MAX_SNAPSHOT + HEADER_SIZE + 16
SNAPSHOT_NAME = re.compile(r"satubrain-([a-f0-9]{32})\.sbrain")


def read_limited(path, maximum):
    with Path(path).open("rb") as source:
        data = source.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("input file exceeds the size limit")
    return data


def atomic_write(path, data, overwrite=True):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".satubrain-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(name, path)
        else:
            os.link(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def crypto():
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:
        raise ValueError("Install the optional sync dependencies with pip install '.[sync]'") from None
    return AESGCM, Scrypt, InvalidTag


def derive(scrypt, password, salt):
    return scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password)


def validate_password(password):
    if not isinstance(password, str) or not 16 <= len(password) <= 1024:
        raise ValueError("sync passphrase must be 16–1024 characters")
    return password.encode("utf-8")


def encode(payload, device, password):
    aes, scrypt, _ = crypto()
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + device.encode("ascii") + salt + nonce
    plaintext = json_bytes(payload)
    if len(plaintext) > MAX_SNAPSHOT:
        raise ValueError("snapshot exceeds the size limit")
    return header + aes(derive(scrypt, password, salt)).encrypt(nonce, plaintext, header)


def decode(blob, device, password):
    aes, scrypt, invalid_tag = crypto()
    header = blob[:HEADER_SIZE]
    if len(blob) < HEADER_SIZE + 16 or len(blob) > MAX_ENVELOPE:
        raise ValueError("invalid snapshot size")
    if not header.startswith(MAGIC + device.encode("ascii")):
        raise ValueError("invalid snapshot version or device ID")
    salt, nonce = header[-28:-12], header[-12:]
    try:
        plaintext = aes(derive(scrypt, password, salt)).decrypt(nonce, blob[HEADER_SIZE:], header)
    except invalid_tag:
        raise ValueError("snapshot authentication failed. Check the passphrase or file integrity.") from None
    return validate_snapshot(json.loads(plaintext))


def push(store, folder, password):
    password = validate_password(password)
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("sync folder must already exist")
    with store.transaction() as db:
        device = store.device(db)
    # Serialize writes from this local database so two pushes can't publish out of order.
    with store.transaction() as db:
        target = folder / f"satubrain-{device}.sbrain"
        atomic_write(target, encode(store.snapshot(db), device, password))
    return {"file": target.name}


def pull(store, folder, password):
    password = validate_password(password)
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("sync folder must already exist")
    paths = []
    for path in folder.iterdir():
        if match := SNAPSHOT_NAME.fullmatch(path.name):
            paths.append((path, match[1]))
            if len(paths) > 32:
                raise ValueError("sync supports up to 32 device snapshots per folder")
    totals = {"files": 0, "added": 0, "tombstones": 0}
    with store.transaction() as db:
        device = store.device(db)
        for path, source in sorted(paths):
            if source == device:
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("device snapshots must be regular files")
            entries, tombstones = decode(read_limited(path, MAX_ENVELOPE), source, password)
            totals["added"] += store._merge(db, entries, tombstones)
            totals["tombstones"] += len(tombstones)
            totals["files"] += 1
    return totals
