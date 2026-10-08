from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import DbSession
from app.audit import write_audit
from app.connectors import ConnectorService
from app.rbac import ConnectorManager
from app.schemas import ConnectorCreate, ConnectorOut, ConnectorSyncOut
from app.tasks import enqueue_connector_sync

router = APIRouter()


@router.get("/connectors", response_model=list[ConnectorOut])
def list_connectors(db: DbSession, auth: ConnectorManager, offset: int = 0, limit: int = 100):
    return ConnectorService().list(db, auth.tenant_id, max(offset, 0), min(max(limit, 1), 500))


@router.post("/connectors", response_model=ConnectorOut, status_code=status.HTTP_201_CREATED)
def create_connector(payload: ConnectorCreate, db: DbSession, auth: ConnectorManager):
    try:
        connector = ConnectorService().create(
            db,
            auth.tenant_id,
            auth.user_id,
            payload.name,
            payload.connector_type,
            payload.configuration,
        )
        write_audit(db, auth, "connector.create", "knowledge_connector", connector.id)
        db.commit()
        return connector
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/connectors/{connector_id}/sync",
    response_model=ConnectorSyncOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_connector_sync(connector_id: str, db: DbSession, auth: ConnectorManager):
    run = ConnectorService().start_sync(db, connector_id, auth.tenant_id, auth.user_id)
    if not run:
        raise HTTPException(status_code=404, detail="连接器不存在或已停用")
    write_audit(db, auth, "connector.sync", "knowledge_connector", connector_id)
    db.commit()
    try:
        enqueue_connector_sync(run.id)
    except Exception as exc:
        ConnectorService().fail_run(db, run.id, "任务队列暂不可用，尚未开始同步")
        raise HTTPException(status_code=503, detail="任务队列暂不可用，请稍后重试") from exc
    return run


@router.get("/connector-syncs/{run_id}", response_model=ConnectorSyncOut)
def get_connector_sync(run_id: str, db: DbSession, auth: ConnectorManager):
    run = ConnectorService().get_run(db, run_id, auth.tenant_id)
    if not run:
        raise HTTPException(status_code=404, detail="同步任务不存在")
    return run
