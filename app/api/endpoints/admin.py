from fastapi import APIRouter
from sqlalchemy import select

from app.api.dependencies import DbSession
from app.audit import write_audit
from app.auth import CurrentUser
from app.models import AnswerFeedback
from app.rbac import SystemAdmin
from app.recommendations import RecommendationService
from app.schemas import FailureCaseOut, RecommendationOut, VectorReconcileOut
from app.services import get_document_service

router = APIRouter()


@router.post("/admin/vector-index/reconcile", response_model=VectorReconcileOut)
def reconcile_vector_index(db: DbSession, auth: SystemAdmin):
    reconciliation_summary = get_document_service().reconcile_vector_index(db)
    write_audit(
        db,
        auth,
        "vector_index.reconcile",
        "vector_index",
        details=reconciliation_summary,
    )
    db.commit()
    return reconciliation_summary


@router.get("/evaluation/failures", response_model=list[FailureCaseOut])
def list_evaluation_failures(db: DbSession, auth: SystemAdmin, limit: int = 500, offset: int = 0):
    return list(
        db.scalars(
            select(AnswerFeedback)
            .where(
                AnswerFeedback.tenant_id == auth.tenant_id,
                AnswerFeedback.score < 0,
            )
            .order_by(AnswerFeedback.created_at.desc())
            .offset(max(offset, 0))
            .limit(min(max(limit, 1), 2000))
        )
    )


@router.get("/recommendations", response_model=list[RecommendationOut])
def recommendations(db: DbSession, auth: CurrentUser, limit: int = 6):
    return RecommendationService().recommend(db, auth, min(max(limit, 1), 20))
