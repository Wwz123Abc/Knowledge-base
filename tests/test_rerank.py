import httpx
from langchain_core.documents import Document

from app.config import Settings
from app.core.rerank import _query_aware_excerpt, build_reranker
from app.core.retrieval import tokenize


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_cross_encoder_api_reranks_documents(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return FakeResponse(
            {
                "results": [
                    {"index": 0, "relevance_score": 0.2},
                    {"index": 1, "relevance_score": 0.9},
                ]
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    settings = Settings(
        reranker_provider="cross_encoder_api",
        reranker_base_url="http://reranker.local/v1",
        reranker_api_key="test-key",
    )
    documents = [Document(page_content="甲"), Document(page_content="乙")]

    reranked = build_reranker("cross_encoder_api", tokenize, settings).rerank("问题", documents)

    assert [document.page_content for document in reranked] == ["乙", "甲"]
    assert reranked[0].metadata["rerank_score"] == 0.9
    assert captured["url"] == "http://reranker.local/v1/rerank"
    assert captured["json"]["model"] == "BAAI/bge-reranker-v2-m3"
    assert all(
        len(document) <= 2 * settings.reranker_max_chars
        for document in captured["json"]["documents"]
    )
    assert captured["headers"]["Authorization"] == "Bearer test-key"


def test_cross_encoder_failure_uses_token_overlap_fallback(monkeypatch):
    request = httpx.Request("POST", "http://reranker.local/rerank")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            httpx.ConnectError("down", request=request)
        ),
    )
    settings = Settings(
        reranker_provider="cross_encoder_api",
        reranker_base_url="http://reranker.local",
    )
    documents = [Document(page_content="无关"), Document(page_content="机械 加工")]

    reranked = build_reranker("cross_encoder_api", tokenize, settings).rerank("机械加工", documents)

    assert reranked[0].page_content == "机械 加工"
    assert "rerank_score" in reranked[0].metadata


def test_query_aware_excerpt_keeps_relevant_text_near_the_end():
    text = "设备工作原理。" * 100 + "压力值在范围内判定 Pass，超出范围判定 Fail。"

    excerpt = _query_aware_excerpt("如何判断 Pass 或 Fail？", text, 128, tokenize)

    assert "Pass" in excerpt
    assert "Fail" in excerpt


def test_query_aware_excerpt_keeps_head_and_relevant_window():
    text = "设备开头信息。" * 30 + "答案在中间：测试数据通过USB传输。" + "后续无关内容。" * 40

    excerpt = _query_aware_excerpt("测试数据如何传输？", text, 64, tokenize)

    assert excerpt.startswith("设备开头信息。")
    assert "USB" in excerpt
    assert len(excerpt) <= 2 * 64
