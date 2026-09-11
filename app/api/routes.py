from fastapi import APIRouter

from app.api.endpoints.admin import router as admin_router
from app.api.endpoints.audit import router as audit_router
from app.api.endpoints.auth_wecom import router as auth_wecom_router
from app.api.endpoints.chat import router as chat_router
from app.api.endpoints.connectors import router as connectors_router
from app.api.endpoints.documents import router as documents_router
from app.api.endpoints.knowledge_bases import router as knowledge_bases_router
from app.api.endpoints.roles import router as roles_router
from app.api.endpoints.system import router as system_router
from app.api.endpoints.tools import router as tools_router

router = APIRouter()
router.include_router(auth_wecom_router)
router.include_router(system_router)
router.include_router(documents_router)
router.include_router(chat_router)
router.include_router(audit_router)
router.include_router(connectors_router)
router.include_router(tools_router)
router.include_router(knowledge_bases_router)
router.include_router(roles_router)
router.include_router(admin_router)
