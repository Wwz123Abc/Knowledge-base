from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import DbSession
from app.audit import write_audit
from app.auth import CurrentUser
from app.rbac import ToolApprover
from app.schemas import (
    ToolApprovalOut,
    ToolDecision,
    ToolDecisionResult,
    ToolRequest,
)
from app.tools import ToolService

router = APIRouter()


@router.post(
    "/tools/requests",
    response_model=ToolApprovalOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_tool(payload: ToolRequest, db: DbSession, auth: CurrentUser):
    try:
        approval = ToolService().submit(
            db, auth.tenant_id, auth.user_id, payload.tool_name, payload.arguments
        )
        write_audit(
            db,
            auth,
            "tool.request",
            "tool_approval",
            approval.id,
            {"tool_name": payload.tool_name},
        )
        db.commit()
        return approval
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tools/approvals", response_model=list[ToolApprovalOut])
def list_tool_approvals(db: DbSession, auth: ToolApprover):
    return ToolService().list_pending(db, auth.tenant_id)


@router.post("/tools/approvals/{approval_id}/decision", response_model=ToolDecisionResult)
def decide_tool(approval_id: str, payload: ToolDecision, db: DbSession, auth: ToolApprover):
    tool_decision_result = ToolService().decide(
        db,
        approval_id,
        auth.tenant_id,
        auth.user_id,
        payload.decision,
        payload.note,
    )
    if not tool_decision_result:
        raise HTTPException(status_code=404, detail="待审批工具请求不存在")
    approval, execution = tool_decision_result
    write_audit(
        db,
        auth,
        f"tool.{payload.decision}",
        "tool_approval",
        approval.id,
        {"execution_status": execution.status if execution else None},
    )
    db.commit()
    return ToolDecisionResult(approval=approval, execution=execution)
