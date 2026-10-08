import json
import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.dependencies import DbSession
from app.audit import write_audit
from app.auth import CurrentUser
from app.cancellation import cancellations
from app.models import AnswerFeedback, RetrievalTrace
from app.schemas import AskRequest, AskResponse, FeedbackIn, FeedbackOut
from app.services import get_rag_service

router = APIRouter()
logger = logging.getLogger("rag.chat")


@router.post("/chat", response_model=AskResponse)
def ask(request: AskRequest, db: DbSession, auth: CurrentUser):
    service = get_rag_service()
    try:
        # Same order as /chat/stream: reject a bad request (400) before reporting that the
        # model isn't configured (503), so the two endpoints answer a bad knowledge_base_ids
        # identically and the 400 doesn't depend on whether an API key happens to be set.
        service.validate_request(db, request, auth)
        return service.ask(db, request, auth)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/chat/stream")
def ask_stream(request: AskRequest, db: DbSession, auth: CurrentUser):
    service = get_rag_service()
    try:
        service.validate_request(db, request, auth)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def event_stream():
        try:
            for event in service.stream(db, request, auth):
                event_name = str(event.get("event", "message"))
                yield f"event: {event_name}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.exception("streaming answer failed")
            # Only messages this application wrote itself (ValueError/RuntimeError, e.g. the
            # circuit breaker's) are safe to show; provider and database errors can carry
            # hostnames, key prefixes and SQL.
            detail = (
                str(exc)
                if isinstance(exc, (ValueError, RuntimeError))
                else "回答生成失败，请稍后重试"
            )
            payload = json.dumps({"event": "error", "detail": detail}, ensure_ascii=False)
            yield f"event: error\ndata: {payload}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/chat/{trace_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_chat(trace_id: str, db: DbSession, auth: CurrentUser):
    trace = db.scalar(
        select(RetrievalTrace).where(
            RetrievalTrace.id == trace_id,
            RetrievalTrace.tenant_id == auth.tenant_id,
            RetrievalTrace.user_id == auth.user_id,
        )
    )
    if not trace:
        raise HTTPException(status_code=404, detail="问答任务不存在")
    cancellations.cancel(trace_id)
    return {"trace_id": trace_id, "status": "cancellation_requested"}


@router.post("/feedback", response_model=FeedbackOut, status_code=status.HTTP_201_CREATED)
def create_feedback(payload: FeedbackIn, db: DbSession, auth: CurrentUser):
    trace = db.scalar(
        select(RetrievalTrace).where(
            RetrievalTrace.id == payload.trace_id,
            RetrievalTrace.tenant_id == auth.tenant_id,
            RetrievalTrace.user_id == auth.user_id,
        )
    )
    if not trace:
        raise HTTPException(status_code=404, detail="问答记录不存在")
    feedback = AnswerFeedback(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        trace_id=trace.id,
        score=payload.score,
        comment=payload.comment,
        question=trace.original_query,
        answer=trace.answer,
    )
    db.add(feedback)
    write_audit(db, auth, "feedback.create", "retrieval_trace", trace.id)
    db.commit()
    db.refresh(feedback)
    return feedback
