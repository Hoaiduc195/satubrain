"""Reject common credential formats before persisting user-supplied text."""

import re

SECRET_FIELDS = (
    r"api[ _-]?key|client[ _-]?secret|password|passwd|pwd|secret|"
    r"token|account[ _-]?key|shared[ _-]?access[ _-]?signature"
)
FIELD_VALUE = r"""(?:"[^"\r\n]+"|'[^'\r\n]+'|[^\s,;}\]'"<>]+)"""
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|"
               r"sk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{20,}|[rs]k_live_[A-Za-z0-9]{16,}|"
               r"xox[baprs]-[A-Za-z0-9-]{12,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE),
    re.compile(rf"""(?:^|[\s{{,;?&])['"]?[\w.-]*(?:{SECRET_FIELDS})['"]?[ \t]*[:=][ \t]*{FIELD_VALUE}""",
               re.IGNORECASE),
    re.compile(r"\b(?:Bearer|Authorization:\s*Basic)\s+[A-Za-z0-9._~+/-]{12,}", re.IGNORECASE),
)


def check_secrets(*values):
    if any(pattern.search(value) for value in values for pattern in SECRET_PATTERNS):
        raise ValueError("Potential credential detected. Remove it before saving this memory.")
