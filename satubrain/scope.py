"""Resolve portable project IDs without storing remote credentials."""

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

LABEL_LIMIT = 240
REMOTE_SCP = re.compile(r"^(?:[^@/]+@)?([^:/]+):([^/].*)$")


def label(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > LABEL_LIMIT:
        raise ValueError(f"{name} must contain 1–{LABEL_LIMIT} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} contains a control character")
    return value.strip()


def git(workspace, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True, text=True, timeout=3, check=True,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def remote_id(remote):
    if not remote:
        return None
    if "://" in remote:
        try:
            url = urlsplit(remote)
            host, path, port = url.hostname, url.path, url.port
        except ValueError:
            return None
        if url.scheme not in {"http", "https", "ssh", "git"} or not host:
            return None
        if port and port != {"http": 80, "https": 443, "ssh": 22, "git": 9418}[url.scheme]:
            host = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    else:
        match = REMOTE_SCP.fullmatch(remote)
        if not match or Path(remote).is_absolute() or len(match[1]) == 1:
            return None
        host, path = match.groups()
        path = path.split("?", 1)[0].split("#", 1)[0]
    repo = path.strip("/").removesuffix(".git")
    return f"{host.lower()}/{repo}" if repo else None


@dataclass(frozen=True)
class Workspace:
    project: str
    branch: str

    @classmethod
    def resolve(cls, workspace=None, project=None, branch=None):
        path = Path(workspace or Path.cwd()).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("workspace must be an existing directory")
        if project is None:
            root = git(path, "rev-parse", "--show-toplevel")
            remote = remote_id(git(path, "config", "--get", "remote.origin.url")) if root else None
            # A local fallback stays isolated even between folders with identical names.
            project = remote or f"local:{root or path}"
        if branch is None:
            branch = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
            if branch is None:
                revision = git(path, "rev-parse", "--verify", "HEAD")
                branch = f"detached/{revision}" if revision else "default"
        return cls(label(project, "project"), label(branch, "branch"))
