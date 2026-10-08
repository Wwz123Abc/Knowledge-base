from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from uuid import uuid4

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.orm import Session

from app.audit import write_audit
from app.auth import AuthContext
from app.cache import get_query_rewrite_cache, get_retrieval_cache
from app.cancellation import cancellations
from app.config import Settings
from app.core.corrective_retrieval import build_corrective_retrieval_graph
from app.core.retrieval import HybridRetriever
from app.costs import estimate_cost, normalize_usage
from app.domain.chat import helpers as chat_helpers
from app.domain.chat.prompts import FALLBACK_SYSTEM_PROMPT, SYSTEM_PROMPT
from app.metrics import ANSWER_DURATION, RETRIEVAL_DURATION
from app.model_router import ModelRouter
from app.models import RetrievalTrace
from app.rbac import SUPER_ADMIN_ROLE
from app.resilience import get_model_circuit_breaker
from app.schemas import AskRequest, AskResponse
from app.security import detect_prompt_injection, detect_prompt_injection_llm, redact_pii

_citation = chat_helpers.citation
_format_context = chat_helpers.format_context
_format_history = chat_helpers.format_history
_groundedness = chat_helpers.groundedness
_is_insufficient_answer = chat_helpers.is_insufficient_answer
_stream_usage = chat_helpers.stream_usage
_usage_record = chat_helpers.usage_record

logger = logging.getLogger("rag.service")

_CANCEL_POLL_SECONDS = 0.25


class RagService:
    def __init__(self, settings: Settings, vector_store):
        self.settings = settings
        self.vector_store = vector_store

    def ask(self, db: Session, request: AskRequest, auth: AuthContext) -> AskResponse:
        if not self.settings.model_ready:
            raise RuntimeError("尚未配置 OPENAI_API_KEY，无法执行问答")
        started = time.perf_counter()
        if self._is_prompt_injection(request.question, auth):
            trace = self._create_trace(db, request, auth, request.question, {}, [])
            trace.answer = "该请求包含试图绕过系统安全规则的指令，无法处理。"
            write_audit(db, auth, "security.prompt_injection", "retrieval_trace", trace.id)
            db.commit()
            return AskResponse(
                trace_id=trace.id,
                answer=trace.answer,
                citations=[],
                insufficient_context=True,
            )
        rewritten_query = self._rewrite_query(request)
        retrieval = self._retrieve(db, rewritten_query, auth, request)
        documents = retrieval.documents
        trace = self._create_trace(db, request, auth, rewritten_query, retrieval.scores, documents)
        if not documents:
            return self._fallback_or_insufficient(db, request, auth, trace, started)

        prompt = self._prompt(request, documents)
        # Release the pooled connection before the (slow) model call; the session reopens a
        # transaction by itself when the answer is written back below.
        db.commit()
        # Route on the whole prompt: the retrieved documents and the history can carry PII
        # even when the question itself doesn't.
        model = self._model(prompt)
        response = get_model_circuit_breaker().call(
            model.invoke,
            [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)],
        )
        answer = response.content if isinstance(response.content, str) else str(response.content)
        answer, invalid_citations = chat_helpers.validate_citation_indices(answer, len(documents))
        insufficient_context = _is_insufficient_answer(answer)
        if insufficient_context:
            return self._fallback_or_insufficient(
                db, request, auth, trace, started, previous_answer=answer
            )
        trace.answer = answer
        trace.groundedness_score = _groundedness(answer, documents)
        trace.latency_ms = int((time.perf_counter() - started) * 1000)
        ANSWER_DURATION.observe(trace.latency_ms / 1000)
        response_metadata = getattr(response, "response_metadata", {}) or {}
        usage = getattr(response, "usage_metadata", None) or response_metadata.get(
            "token_usage", {}
        )
        model_name = str(response_metadata.get("model_name") or self.settings.chat_model)
        trace.token_usage = {
            **normalize_usage(usage),
            "model": model_name,
            "estimated_cost_usd": estimate_cost(self.settings, model_name, usage),
        }
        if invalid_citations:
            trace.retrieval_scores = {
                **trace.retrieval_scores,
                "citation_validation": {"invalid_indices": invalid_citations},
            }
        write_audit(
            db,
            auth,
            "chat.answer",
            "retrieval_trace",
            trace.id,
            {"chunks": [doc.metadata.get("chunk_id") for doc in documents]},
        )
        db.commit()
        return AskResponse(
            trace_id=trace.id,
            answer=answer,
            citations=[] if insufficient_context else [_citation(doc) for doc in documents],
            insufficient_context=insufficient_context,
        )

    @staticmethod
    def validate_request(db: Session, request: AskRequest, auth: AuthContext) -> None:
        if not request.knowledge_base_ids:
            return
        from app.knowledge_bases import KnowledgeBaseService

        KnowledgeBaseService().validate_ids(db, auth.tenant_id, request.knowledge_base_ids)

    def stream(
        self, db: Session, request: AskRequest, auth: AuthContext
    ) -> Iterator[dict[str, object]]:
        if not self.settings.model_ready:
            raise RuntimeError("尚未配置 OPENAI_API_KEY，无法执行问答")
        started = time.perf_counter()
        if self._is_prompt_injection(request.question, auth):
            trace = self._create_trace(db, request, auth, request.question, {}, [])
            trace.answer = "该请求包含试图绕过系统安全规则的指令，无法处理。"
            write_audit(db, auth, "security.prompt_injection", "retrieval_trace", trace.id)
            db.commit()
            yield {"event": "metadata", "trace_id": trace.id, "citations": []}
            yield {"event": "token", "content": trace.answer}
            yield {"event": "done", "insufficient_context": True}
            return
        rewritten_query = self._rewrite_query(request)
        retrieval = self._retrieve(db, rewritten_query, auth, request)
        documents = retrieval.documents
        trace = self._create_trace(db, request, auth, rewritten_query, retrieval.scores, documents)
        citations = [_citation(doc).model_dump() for doc in documents]
        # Read the id *before* committing: after the commit the instance is expired, and
        # touching any attribute (even the id) issues a SELECT that reopens a transaction —
        # which would keep one pooled connection checked out for the whole LLM stream and cap
        # concurrent answers at the pool size. The commit is what releases the connection.
        trace_id = trace.id
        db.commit()
        yield {"event": "metadata", "trace_id": trace_id, "citations": citations}
        if not documents:
            fallback_response = self._fallback_or_insufficient(db, request, auth, trace, started)
            yield {"event": "token", "content": fallback_response.answer}
            yield {
                "event": "done",
                "insufficient_context": True,
                "fallback": fallback_response.fallback,
            }
            return

        prompt = self._prompt(request, documents)
        answer_parts: list[str] = []
        streamed_usage: dict[str, int] = {}
        streamed_model = self.settings.chat_model
        # If the client disconnects mid-stream, FastAPI/Starlette stops consuming this
        # generator, which Python closes by raising GeneratorExit at whatever `yield`
        # it's paused on — that unwinds straight through the loop below without
        # running any of the normal completion/cancellation code paths. The `finally`
        # covers exactly that case: it only does anything when none of those paths
        # already ran (`stream_completed` stays False), so a disconnect still gets the
        # partial answer persisted and an audit entry instead of leaving the trace
        # stuck holding only metadata forever.
        stream_completed = False
        last_cancel_check = 0.0
        try:
            # The streaming call goes through the same circuit breaker as the blocking ones,
            # so a model outage that only shows up on streamed answers still trips it.
            with get_model_circuit_breaker().guard():
                for chunk in self._model(prompt).stream(
                    [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
                ):
                    streamed_usage, streamed_model = _stream_usage(
                        chunk, streamed_usage, streamed_model
                    )
                    # Polling Redis for every single token added a round trip per token;
                    # a stop request only needs to be noticed within a fraction of a second.
                    now = time.monotonic()
                    if now - last_cancel_check >= _CANCEL_POLL_SECONDS:
                        last_cancel_check = now
                        if cancellations.is_cancelled(trace_id):
                            stream_completed = True
                            trace.answer = "".join(answer_parts)
                            trace.latency_ms = int((time.perf_counter() - started) * 1000)
                            trace.token_usage = _usage_record(
                                self.settings, streamed_model, streamed_usage
                            )
                            write_audit(db, auth, "chat.cancel", "retrieval_trace", trace.id)
                            db.commit()
                            cancellations.clear(trace_id)
                            yield {"event": "done", "cancelled": True}
                            return
                    content = chunk.content if isinstance(chunk.content, str) else ""
                    if content:
                        answer_parts.append(content)
                        yield {"event": "token", "content": content}
            stream_completed = True
        finally:
            if not stream_completed:
                trace.answer = "".join(answer_parts)
                trace.latency_ms = int((time.perf_counter() - started) * 1000)
                trace.token_usage = _usage_record(self.settings, streamed_model, streamed_usage)
                write_audit(db, auth, "chat.interrupted", "retrieval_trace", trace.id)
                db.commit()
                cancellations.clear(trace_id)
        trace.answer = "".join(answer_parts)
        insufficient_context = _is_insufficient_answer(trace.answer)
        _validated_answer, invalid_citations = chat_helpers.validate_citation_indices(
            trace.answer, len(documents)
        )
        if insufficient_context:
            fallback_response = self._fallback_or_insufficient(
                db, request, auth, trace, started, previous_answer=trace.answer
            )
            cancellations.clear(trace_id)
            if fallback_response.fallback:
                # The refusal text was already streamed token by token; tell the client to
                # drop it so the user doesn't see it glued in front of the fallback answer.
                yield {"event": "reset"}
                yield {"event": "token", "content": fallback_response.answer}
                yield {"event": "done", "insufficient_context": True, "fallback": True}
            else:
                yield {"event": "done", "insufficient_context": True, "fallback": False}
            return
        trace.groundedness_score = _groundedness(trace.answer, documents)
        trace.latency_ms = int((time.perf_counter() - started) * 1000)
        trace.token_usage = _usage_record(self.settings, streamed_model, streamed_usage)
        write_audit(db, auth, "chat.stream", "retrieval_trace", trace.id)
        db.commit()
        cancellations.clear(trace_id)
        yield {
            "event": "done",
            "insufficient_context": insufficient_context,
            "invalid_citations": invalid_citations,
        }

    def _is_prompt_injection(self, text: str, auth: AuthContext) -> bool:
        if detect_prompt_injection(text):
            return True
        # The regex/marker rules can miss a disguised, encoded, or multilingual
        # attempt. A second LLM-judged pass catches more of those, but costs real
        # latency per call, so it's only worth paying for admin-level sessions —
        # the higher blast radius (an admin token has document.manage / role.manage
        # reach) is what makes that extra cost worth it, not every anonymous question.
        if SUPER_ADMIN_ROLE in auth.roles or "admin" in auth.roles:
            return detect_prompt_injection_llm(text, self._model())
        return False

    def _rewrite_query(self, request: AskRequest) -> str:
        if not self.settings.enable_query_rewrite or not request.conversation_history:
            return request.question
        history = _format_history(request.conversation_history)
        rewrite_cache = get_query_rewrite_cache()
        cache_key = rewrite_cache.key(request.question, history)
        cached = rewrite_cache.get(cache_key)
        if cached is not None:
            return cached
        message = (
            "把用户的追问改写成独立、可检索的问题。只输出改写后的问题，不要回答。\n"
            f"对话：\n{history}\n追问：{request.question}"
        )
        try:
            response = get_model_circuit_breaker().call(
                self._model().invoke, [HumanMessage(content=message)]
            )
            content = response.content if isinstance(response.content, str) else ""
            rewritten = content.strip() or request.question
        except Exception:
            logger.warning("Query rewrite failed; continuing with the original question")
            return request.question
        rewrite_cache.set(cache_key, rewritten)
        return rewritten

    def _model(self, content: str = ""):
        return ModelRouter(self.settings).build(content)

    def _direct_answer(self, question: str) -> tuple[str, dict, str]:
        """Answer directly from the LLM with the fallback (non-KB) system prompt."""
        model = self._model(question)
        response = get_model_circuit_breaker().call(
            model.invoke,
            [SystemMessage(content=FALLBACK_SYSTEM_PROMPT), HumanMessage(content=question)],
        )
        text = response.content if isinstance(response.content, str) else str(response.content)
        metadata = getattr(response, "response_metadata", {}) or {}
        usage = getattr(response, "usage_metadata", None) or metadata.get("token_usage", {})
        model_name = str(metadata.get("model_name") or self.settings.chat_model)
        return text.strip(), usage, model_name

    def _fallback_or_insufficient(
        self,
        db: Session,
        request: AskRequest,
        auth: AuthContext,
        trace: RetrievalTrace,
        started: float,
        previous_answer: str | None = None,
    ) -> AskResponse:
        """Answer an unanswerable question via the LLM, clearly marked as non-official.

        Returns a fallback answer (fallback=True) when enabled and the model call
        succeeds; otherwise returns the standard insufficient response.
        """
        fallback_text: str | None = None
        if self.settings.unanswerable_fallback_enabled:
            try:
                fallback_text, usage, model_name = self._direct_answer(request.question)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Unanswerable fallback failed; returning insufficient: %s", exc)
                fallback_text = None
        if fallback_text:
            answer = f"{self.settings.unanswerable_fallback_prefix}\n{fallback_text}"
            trace.answer = answer
            trace.latency_ms = int((time.perf_counter() - started) * 1000)
            trace.token_usage = {
                **normalize_usage(usage),
                "model": model_name,
                "estimated_cost_usd": estimate_cost(self.settings, model_name, usage),
                "fallback": True,
            }
            write_audit(
                db,
                auth,
                "chat.fallback",
                "retrieval_trace",
                trace.id,
                {
                    "question": (
                        redact_pii(request.question)
                        if self.settings.redact_audit_pii
                        else request.question
                    ),
                    "fallback": True,
                },
            )
            db.commit()
            return AskResponse(
                trace_id=trace.id,
                answer=answer,
                citations=[],
                insufficient_context=True,
                fallback=True,
            )
        answer = previous_answer or "知识库中没有找到足够依据。"
        trace.answer = answer
        trace.latency_ms = int((time.perf_counter() - started) * 1000)
        write_audit(db, auth, "chat.insufficient", "retrieval_trace", trace.id, {"fallback": False})
        db.commit()
        return AskResponse(
            trace_id=trace.id,
            answer=answer,
            citations=[],
            insufficient_context=True,
            fallback=False,
        )

    def _retrieve(self, db: Session, query: str, auth: AuthContext, request: AskRequest):
        from app.knowledge_bases import KnowledgeBaseService

        groups = list(auth.groups)
        knowledge_base_service = KnowledgeBaseService()
        if request.knowledge_base_ids:
            knowledge_base_ids = knowledge_base_service.validate_ids(
                db, auth.tenant_id, request.knowledge_base_ids
            )
        else:
            knowledge_base_ids = knowledge_base_service.route(db, auth.tenant_id, query)
        cache = get_retrieval_cache()
        key = cache.key(query, auth.tenant_id, groups, knowledge_base_ids)
        cached = cache.get(key)
        if cached:
            return cached
        retriever = HybridRetriever(self.settings, self.vector_store)

        def retrieve(candidate_query: str):
            with RETRIEVAL_DURATION.time():
                return retriever.retrieve(
                    db, candidate_query, auth.tenant_id, groups, knowledge_base_ids
                )

        graph = build_corrective_retrieval_graph(
            retrieve,
            self._rewrite_for_retry,
            max_attempts=2,
            min_rerank_score=self.settings.corrective_min_rerank_score,
        )
        retrieval_state = graph.invoke({"query": query, "attempts": 0})
        retrieval_result = retrieval_state["result"]
        if retrieval_result.documents:
            cache.set(key, retrieval_result)
        return retrieval_result

    def _rewrite_for_retry(self, query: str) -> str:
        message = (
            "为企业知识库检索改写下面的问题，补充同义词或全称，但不要回答。"
            f"只输出一个改写后的查询：{query}"
        )
        try:
            response = get_model_circuit_breaker().call(
                self._model().invoke, [HumanMessage(content=message)]
            )
            content = response.content if isinstance(response.content, str) else ""
            return content.strip() or query
        except Exception:
            logger.warning("Corrective rewrite failed; continuing with the current query")
            return query

    @staticmethod
    def _prompt(request: AskRequest, documents: list[Document]) -> str:
        context = _format_context(documents)
        history = _format_history(request.conversation_history)
        return f"""最近对话：
{history or "无"}

知识库资料：
{context}

用户问题：{request.question}

请给出有依据的回答，并用 [序号] 引用资料。"""

    def _create_trace(
        self,
        db: Session,
        request: AskRequest,
        auth: AuthContext,
        rewritten_query: str,
        scores: dict,
        documents: list[Document],
    ) -> RetrievalTrace:
        trace = RetrievalTrace(
            id=str(uuid4()),
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            original_query=(
                redact_pii(request.question) if self.settings.redact_audit_pii else request.question
            ),
            rewritten_query=(
                redact_pii(rewritten_query) if self.settings.redact_audit_pii else rewritten_query
            ),
            retrieved_chunk_ids=[str(doc.metadata.get("chunk_id")) for doc in documents],
            retrieval_scores=scores,
        )
        db.add(trace)
        return trace
