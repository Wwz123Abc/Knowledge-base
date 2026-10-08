from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.api.dependencies import DbSession
from app.config import Settings, get_settings

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class AuthContext:
    user_id: str
    display_name: str
    tenant_id: str
    groups: tuple[str, ...]
    roles: tuple[str, ...]


@lru_cache(maxsize=8)
def _jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True)


def _claim_list(claims: dict[str, Any], name: str) -> tuple[str, ...]:
    value = claims.get(name, [])
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if item)


def get_auth_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
    db: DbSession,
) -> AuthContext:
    if settings.auth_mode == "dev":
        return AuthContext(
            user_id=settings.dev_user_id,
            display_name=settings.dev_user_name,
            tenant_id=settings.dev_tenant_id,
            groups=tuple(settings.dev_group_list),
            roles=tuple(settings.dev_role_list),
        )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少身份令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if settings.auth_mode == "wecom":
        # 企业微信不签发标准 OIDC JWT，这里验证的是登录时后端自己签发的会话令牌
        # （见 app/wecom.py 的 mint_session_token），claim 名称是我们自己定的，跟
        # OIDC 分支保持一致（sub/name/tenant_id/groups/roles），所以下面能直接复用
        # _claim_list。签名用共享密钥（HS256），不需要 JWKS。
        if not settings.wecom_session_secret:
            raise HTTPException(status_code=503, detail="企业微信登录未配置")
        try:
            claims = jwt.decode(
                credentials.credentials,
                settings.wecom_session_secret,
                algorithms=["HS256"],
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="登录会话无效或已过期，请重新登录",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        user_id = str(claims["sub"])
        tenant_id = str(claims.get("tenant_id") or "").strip() or settings.wecom_tenant_id
        # Roles granted through the admin UI are read from the database on every request, not
        # baked into the token at login: removing someone's role (or the person leaving)
        # takes effect immediately instead of when their 8-hour session happens to expire.
        from app.rbac import role_names_assigned_to

        granted = [
            name for name in role_names_assigned_to(db, tenant_id, user_id) if name != "super_admin"
        ]
        return AuthContext(
            user_id=user_id,
            display_name=str(claims.get("name") or claims["sub"]),
            tenant_id=tenant_id,
            groups=_claim_list(claims, "groups"),
            roles=tuple(dict.fromkeys([*_claim_list(claims, "roles"), *granted])),
        )

    if not settings.oidc_jwks_url or not settings.oidc_issuer or not settings.oidc_audience:
        raise HTTPException(status_code=503, detail="OIDC 配置不完整")
    try:
        signing_key = _jwks_client(settings.oidc_jwks_url).get_signing_key_from_jwt(
            credentials.credentials
        )
        claims = jwt.decode(
            credentials.credentials,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="身份令牌无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    tenant_id = str(claims.get(settings.oidc_tenant_claim, "")).strip()
    if not tenant_id:
        raise HTTPException(status_code=403, detail="身份令牌缺少租户信息")
    return AuthContext(
        user_id=str(claims["sub"]),
        display_name=str(claims.get("name") or claims.get("preferred_username") or claims["sub"]),
        tenant_id=tenant_id,
        groups=_claim_list(claims, settings.oidc_groups_claim),
        roles=_claim_list(claims, settings.oidc_roles_claim),
    )


CurrentUser = Annotated[AuthContext, Depends(get_auth_context)]
