from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import jwt

from app.config import Settings

WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
WECOM_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"

# WeCom's own access_token is valid ~2 hours and shared across every user login on this
# corp/app; fetching a fresh one per login would burn into WeCom's per-app rate limit under
# real traffic, so it's cached in-process with a safety margin before the real expiry.
_token_cache: dict[str, tuple[str, float]] = {}


# access_token invalid / expired / credential errors: the cached token is dead, fetch a new one.
_TOKEN_ERRCODES = {40001, 40014, 42001}


class WeComError(Exception):
    """A WeCom API call returned errcode != 0, or the HTTP call itself failed."""

    def __init__(self, message: str, errcode: int | None = None):
        super().__init__(message)
        self.errcode = errcode


@dataclass(frozen=True, slots=True)
class WeComIdentity:
    userid: str
    display_name: str
    department_ids: list[int]


def _require_configured(settings: Settings) -> None:
    if not (settings.wecom_corp_id and settings.wecom_agent_id and settings.wecom_secret):
        raise WeComError("企业微信登录未配置 WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_SECRET")
    if not settings.wecom_session_secret:
        raise WeComError("未配置 WECOM_SESSION_SECRET，无法签发登录会话")


def build_authorize_url(settings: Settings, state: str) -> str:
    _require_configured(settings)
    if not settings.wecom_redirect_uri:
        raise WeComError("未配置 WECOM_REDIRECT_URI")
    params = httpx.QueryParams(
        {
            "appid": settings.wecom_corp_id,
            "redirect_uri": settings.wecom_redirect_uri,
            "response_type": "code",
            "scope": "snsapi_base",
            "state": state,
            "agentid": settings.wecom_agent_id,
        }
    )
    return f"{WECOM_AUTHORIZE_URL}?{params}#wechat_redirect"


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, str]) -> dict:
    try:
        response = await client.get(f"{WECOM_API_BASE}{path}", params=params, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise WeComError(f"调用企业微信接口失败：{exc}") from exc
    payload = response.json()
    if payload.get("errcode"):
        raise WeComError(
            f"企业微信接口返回错误：{payload.get('errcode')} {payload.get('errmsg')}",
            errcode=payload.get("errcode"),
        )
    return payload


async def fetch_access_token(settings: Settings, client: httpx.AsyncClient) -> str:
    _require_configured(settings)
    cached = _token_cache.get(settings.wecom_corp_id)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    payload = await _get(
        client,
        "/gettoken",
        {"corpid": settings.wecom_corp_id, "corpsecret": settings.wecom_secret},
    )
    token = payload["access_token"]
    # Refresh 5 minutes before WeCom's own expiry to avoid using a token that dies mid-request.
    expires_at = time.monotonic() + max(int(payload.get("expires_in", 7200)) - 300, 60)
    _token_cache[settings.wecom_corp_id] = (token, expires_at)
    return token


async def exchange_code_for_identity(
    settings: Settings, code: str, client: httpx.AsyncClient | None = None
) -> WeComIdentity:
    """Exchange a WeCom OAuth `code` for the member's userid, name and department IDs."""
    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        for attempt in (1, 2):
            access_token = await fetch_access_token(settings, client)
            try:
                userinfo = await _get(
                    client, "/user/getuserinfo", {"access_token": access_token, "code": code}
                )
                userid = userinfo.get("UserId")
                if not userid:
                    raise WeComError("该用户未在企业微信通讯录中，无法确定身份")
                detail = await _get(
                    client, "/user/get", {"access_token": access_token, "userid": userid}
                )
                break
            except WeComError as exc:
                # A token WeCom has invalidated early (secret rotated, token refreshed
                # elsewhere) would otherwise stay cached until its local expiry and fail
                # every login for up to two hours. Drop it and retry once with a fresh one.
                if exc.errcode in _TOKEN_ERRCODES and attempt == 1:
                    _token_cache.pop(settings.wecom_corp_id, None)
                    continue
                raise
        return WeComIdentity(
            userid=userid,
            display_name=detail.get("name") or userid,
            department_ids=[int(item) for item in detail.get("department") or []],
        )
    finally:
        if owns_client:
            await client.aclose()


async def fetch_departments(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> list[dict]:
    """The corp's full department tree, for populating a department picker instead of
    leaving it as free text that never matches the company's real org structure."""
    _require_configured(settings)
    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        access_token = await fetch_access_token(settings, client)
        payload = await _get(client, "/department/list", {"access_token": access_token})
        return [
            {
                "id": item["id"],
                "name": item["name"],
                "parent_id": item.get("parentid", 0),
            }
            for item in payload.get("department", [])
        ]
    finally:
        if owns_client:
            await client.aclose()


def resolve_roles(
    settings: Settings, userid: str, db_role_names: list[str] | None = None
) -> list[str]:
    # super_admin stays a pure server-config allowlist (see UserRoleAssignment / RoleService.
    # assign_user_role, which deliberately refuses to grant it) — the same "not just data"
    # protection as the super_admin bypass in app/rbac.py, so it can't be handed out by
    # anyone who only has role.manage.
    if userid in settings.wecom_super_admin_userid_list:
        return ["super_admin"]
    roles = ["user"]
    if userid in settings.wecom_admin_userid_list:
        roles.append("admin")
    for name in db_role_names or []:
        # RoleService.assign_user_role already refuses to create a super_admin assignment,
        # so this shouldn't be reachable in practice — kept as a second, independent gate
        # directly at the point roles get minted into a token, since that's the one place
        # a mistake here would actually matter.
        if name == "super_admin" or name in roles:
            continue
        roles.append(name)
    return roles


def department_groups(department_ids: list[int]) -> list[str]:
    return [f"dept_{department_id}" for department_id in department_ids]


def mint_session_token(
    settings: Settings, identity: WeComIdentity, db_role_names: list[str] | None = None
) -> str:
    _require_configured(settings)
    now = datetime.now(UTC)
    claims = {
        "sub": identity.userid,
        "name": identity.display_name,
        "tenant_id": settings.wecom_tenant_id,
        "groups": department_groups(identity.department_ids),
        "roles": resolve_roles(settings, identity.userid, db_role_names),
        "iat": now,
        "exp": now + timedelta(minutes=settings.wecom_session_ttl_minutes),
    }
    return jwt.encode(claims, settings.wecom_session_secret, algorithm="HS256")


def mint_state_token(settings: Settings) -> str:
    """A short-lived, self-verifying CSRF token for the login redirect round trip.

    Sent to WeCom as `state` and also stashed in an httponly cookie; the callback checks
    the two match (double-submit) before trusting the `code`, without needing server-side
    session storage for the handful of minutes a login takes.
    """
    _require_configured(settings)
    now = datetime.now(UTC)
    claims = {"purpose": "wecom_login_state", "iat": now, "exp": now + timedelta(minutes=10)}
    return jwt.encode(claims, settings.wecom_session_secret, algorithm="HS256")


def verify_state_token(settings: Settings, token: str) -> bool:
    try:
        claims = jwt.decode(token, settings.wecom_session_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return False
    return claims.get("purpose") == "wecom_login_state"
