from __future__ import annotations

import re

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    tokens = [token.lower() for token in TOKEN_PATTERN.findall(text)]
    chinese = [token for token in tokens if "\u4e00" <= token <= "\u9fff"]
    tokens.extend(a + b for a, b in zip(chinese, chinese[1:], strict=False))
    return tokens


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
