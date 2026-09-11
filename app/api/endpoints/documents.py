from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.api.dependencies import DbSession
from app.audit import write_audit
from app.auth import CurrentUser
from app.rbac import DocumentManager
from app.schemas import (
    DocumentOut,
    DocumentUpdate,
    JobOut,
    UploadAccepted,
    VersionOut,
    VersionUpdate,
)
from app.services import get_document_service
from app.tasks import enqueue_ingestion

router = APIRouter()


@router.get("/documents", response_model=list[DocumentOut])
def list_documents(db: DbSession, auth: CurrentUser, offset: int = 0, limit: int = 100):
    return get_document_service().list_documents(
        db, auth.tenant_id, max(offset, 0), min(max(limit, 1), 500)
    )


@router.post("/documents", response_model=UploadAccepted, status_code=status.HTTP_202_ACCEPTED)
def upload_document(
    file: Annotated[UploadFile, File()],
    db: DbSession,
    auth: DocumentManager,
    title: Annotated[str | None, Form()] = None,
    department: Annotated[str | None, Form()] = None,
    access_groups: Annotated[str | None, Form()] = None,
    knowledge_base_ids: Annotated[str | None, Form()] = None,
):
    groups = [item.strip() for item in (access_groups or "").split(",") if item.strip()]
    base_ids = [item.strip() for item in (knowledge_base_ids or "").split(",") if item.strip()]
    try:
        document, job = get_document_service().stage_upload(
            db,
            file,
            title,
            department,
            groups,
            auth.tenant_id,
            auth.user_id,
            base_ids,
        )
        write_audit(
            db,
            auth,
            "document.upload",
            "knowledge_document",
            document.id,
            {
                "filename": document.filename,
                "access_groups": groups,
                "knowledge_base_ids": base_ids,
                "job_id": job.id,
            },
        )
        db.commit()
        enqueue_ingestion(job.id)
        return UploadAccepted(document=document, job=job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/versions/{version_id}", response_model=VersionOut)
def update_document_version(
    version_id: str, payload: VersionUpdate, db: DbSession, auth: DocumentManager
):
    try:
        version = get_document_service().update_version(
            db,
            version_id,
            auth.tenant_id,
            payload.status,
            payload.owner_id,
            payload.valid_from,
            payload.valid_until,
        )
        if not version:
            raise HTTPException(status_code=404, detail="文档版本不存在")
        write_audit(
            db,
            auth,
            "document.version.update",
            "document_version",
            version.id,
            payload.model_dump(exclude_unset=True, mode="json"),
        )
        db.commit()
        return version
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: str, db: DbSession, auth: DocumentManager):
    try:
        if not get_document_service().delete_document(db, document_id, auth.tenant_id):
            raise HTTPException(status_code=404, detail="文档不存在")
        write_audit(db, auth, "document.delete", "knowledge_document", document_id)
        db.commit()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.patch("/documents/{document_id}", response_model=DocumentOut)
def update_document(
    document_id: str, payload: DocumentUpdate, db: DbSession, auth: DocumentManager
):
    document_update_result = get_document_service().update_document(
        db,
        document_id,
        auth.tenant_id,
        payload.title,
        payload.department,
        payload.access_groups,
        payload.knowledge_base_ids,
    )
    if not document_update_result:
        raise HTTPException(status_code=404, detail="文档不存在")
    document, job = document_update_result
    write_audit(
        db,
        auth,
        "document.update",
        "knowledge_document",
        document.id,
        payload.model_dump(exclude_unset=True),
    )
    db.commit()
    if job:
        enqueue_ingestion(job.id)
    return document


@router.get("/documents/{document_id}/versions", response_model=list[VersionOut])
def list_document_versions(document_id: str, db: DbSession, auth: CurrentUser):
    return get_document_service().list_versions(db, document_id, auth.tenant_id)


@router.post(
    "/documents/{document_id}/versions",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def upload_document_version(
    document_id: str,
    file: Annotated[UploadFile, File()],
    db: DbSession,
    auth: DocumentManager,
):
    try:
        document, job = get_document_service().stage_new_version(
            db, document_id, file, auth.tenant_id, auth.user_id
        )
        write_audit(
            db,
            auth,
            "document.version.upload",
            "knowledge_document",
            document.id,
            {"job_id": job.id, "filename": document.filename},
        )
        db.commit()
        enqueue_ingestion(job.id)
        return UploadAccepted(document=document, job=job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: DbSession, auth: CurrentUser):
    job = get_document_service().get_job(db, job_id, auth.tenant_id)
    if not job:
        raise HTTPException(status_code=404, detail="入库任务不存在")
    return job


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: str, db: DbSession, auth: DocumentManager):
    job = get_document_service().retry_job(db, job_id, auth.tenant_id)
    if not job:
        raise HTTPException(status_code=409, detail="任务不存在或当前状态不可重试")
    write_audit(db, auth, "ingestion.retry", "ingestion_job", job.id)
    db.commit()
    enqueue_ingestion(job.id)
    return job


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, db: DbSession, auth: DocumentManager):
    job = get_document_service().cancel_job(db, job_id, auth.tenant_id)
    if not job:
        raise HTTPException(status_code=409, detail="任务不存在或当前状态不可取消")
    write_audit(db, auth, "ingestion.cancel", "ingestion_job", job.id)
    db.commit()
    return job
