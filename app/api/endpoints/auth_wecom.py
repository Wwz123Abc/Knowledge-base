import logging
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.api.dependencies import DbSession, SettingsDep
from app.auth import CurrentUser
from app.rbac import role_names_assigned_to
from app.schemas import WeComDepartmentOut, WeComQrConfigOut
from app.wecom import (
    WeComError,
    build_authorize_url,
    exchange_code_for_identity,
    fetch_departments,
    mint_session_token,
    mint_state_token,
    verify_state_token,
)

logger = logging.getLogger("rag.auth.wecom")
router = APIRouter()


@router.get("/auth/wecom/login")
def wecom_login(settings: SettingsDep) -> RedirectResponse:
    # `state` is a short-lived, server-secret-signed token (see mint_state_token): WeCom
    # echoes it back verbatim on the callback, and because forging a valid one requires
    # knowing wecom_session_secret, that alone is enough CSRF protection for this round
    # trip — no server-side session storage needed for a login that takes a few seconds.
    try:
        state = mint_state_token(settings)
        authorize_url = build_authorize_url(settings, state)
    except WeComError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(authorize_url, status_code=307)


@router.get("/auth/wecom/qr-config", response_model=WeComQrConfigOut)
def wecom_qr_config(settings: SettingsDep) -> WeComQrConfigOut:
    # Desktop WeCom hands external links straight to the system browser instead of an
    # in-client webview, so the snsapi_base silent-auth flow above (mobile-only) can't work
    # there — WeCom rejects it with "请在企业微信客户端打开链接". Desktop instead gets the
    # official QR-scan widget (wwLogin JS SDK), which the frontend initializes with these
    # values; scanning redirects through the exact same /callback endpoint above, so no
    # separate backend handling is needed for this path.
    if not (settings.wecom_corp_id and settings.wecom_agent_id and settings.wecom_redirect_uri):
        raise HTTPException(status_code=503, detail="企业微信登录未配置")
    try:
        state = mint_state_token(settings)
    except WeComError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return WeComQrConfigOut(
        corp_id=settings.wecom_corp_id,
        agent_id=settings.wecom_agent_id,
        redirect_uri=settings.wecom_redirect_uri,
        state=state,
    )


_department_cache: dict[str, tuple[list[dict], float]] = {}
_DEPARTMENT_CACHE_TTL_SECONDS = 300


@router.get("/wecom/departments", response_model=list[WeComDepartmentOut])
async def wecom_departments(settings: SettingsDep, auth: CurrentUser):
    # Any authenticated user can read this — it's just the corp's org chart, used to
    # populate a department picker on upload/edit forms instead of leaving "所属部门" as
    # free text that drifts from the real department names over time.
    cached = _department_cache.get(settings.wecom_corp_id or "")
    if cached and cached[1] > time.monotonic():
        return cached[0]
    try:
        departments = await fetch_departments(settings)
    except WeComError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _department_cache[settings.wecom_corp_id or ""] = (
        departments,
        time.monotonic() + _DEPARTMENT_CACHE_TTL_SECONDS,
    )
    return departments


@router.get("/auth/wecom/callback")
async def wecom_callback(
    settings: SettingsDep,
    db: DbSession,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    if error or not code or not state:
        return RedirectResponse(f"/?auth_error={error or 'missing_code'}", status_code=307)
    if not verify_state_token(settings, state):
        return RedirectResponse("/?auth_error=invalid_state", status_code=307)
    try:
        identity = await exchange_code_for_identity(settings, code)
        db_roles = role_names_assigned_to(db, settings.wecom_tenant_id, identity.userid)
        token = mint_session_token(settings, identity, db_roles)
    except WeComError as exc:
        logger.warning("WeCom login failed: %s", exc)
        return RedirectResponse("/?auth_error=wecom_failed", status_code=307)
    # The token rides in the URL fragment (#), not the query string: fragments never get
    # sent to the server on the next request or land in access logs / Referer headers.
    # The frontend picks it up from location.hash on load and moves it into localStorage.
    return RedirectResponse(f"/#wecom_token={token}", status_code=307)
