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


_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_WORD = re.compile(r"[A-Za-z0-9_.-]{2,}")


def lexical_tokens(text: str, limit: int = 24) -> list[str]:
    """Terms for the substring (ILIKE) search channel.

    `tokenize()` emits every single Chinese character first and the two-character pairs
    after, so capping it at 24 kept almost only single characters ("的", "年") for any
    question longer than a dozen characters — each of which matches nearly every chunk.
    Here Chinese runs contribute overlapping three-character windows instead: specific
    enough to mean something, and long enough for pg_trgm's GIN index to serve them (the
    index can't help with patterns shorter than three characters).
    """
    terms: list[str] = [word.lower() for word in _ASCII_WORD.findall(text)]
    for run in _CJK_RUN.findall(text):
        if len(run) >= 3:
            terms.extend(run[index : index + 3] for index in range(len(run) - 2))
        elif len(run) == 2:
            terms.append(run)
    unique = list(dict.fromkeys(terms))
    if unique:
        return unique[:limit]
    return list(dict.fromkeys(tokenize(text)))[:limit]
