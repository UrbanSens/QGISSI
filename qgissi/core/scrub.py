"""Remove secrets from any text before it is sent to a model provider."""
from __future__ import annotations

import re

_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"), "sk-ant-***"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), "sk-***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "AIza***"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"), "gh_***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{12,}=*"), "Bearer ***"),
    (re.compile(r"(?i)\b(password|passwd|pwd|token|access_token|apikey|api_key|secret|authcfg)=([^&\s'\"|]+)"),
     r"\1=***"),
    (re.compile(r"(?i)(://[^:/\s@]+:)[^@\s/]+@"), r"\1***@"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[private key removed]"),
]


def scrub(text: str) -> str:
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
