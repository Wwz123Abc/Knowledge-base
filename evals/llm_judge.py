from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage

from app.config import Settings, get_settings
from app.model_router import ModelRouter


def judge_groundedness(
    question: str,
    answer: str,
    citations: list[dict],
    settings: Settings | None = None,
) -> float:
    settings = settings or get_settings()
    context = "\n\n".join(str(item.get("excerpt", "")) for item in citations)
    prompt = f"""你是知识库问答忠实度评审。只判断回答中的事实是否由资料支持。
返回 JSON：{{"score": 0到1之间的小数}}。不要输出其他文字。

问题：{question}
回答：{answer}
资料：{context}"""
    try:
        response = ModelRouter(settings).build(question).invoke([HumanMessage(content=prompt)])
        content = response.content if isinstance(response.content, str) else str(response.content)
        return parse_judge_score(content)
    except Exception:
        return 0.0


def parse_judge_score(content: str) -> float:
    try:
        payload = json.loads(content)
        value = float(payload["score"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        match = re.search(r"(?:score|分数)[^0-9]*([01](?:\.\d+)?)", content, re.IGNORECASE)
        if not match:
            raise ValueError("LLM judge did not return a score") from None
        value = float(match.group(1))
    return min(max(value, 0.0), 1.0)
