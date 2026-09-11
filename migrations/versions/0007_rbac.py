"""Add RBAC (roles, permissions, role_permissions) and seed the default system roles.

Revision ID: 0007_rbac
Revises: 0006_embedding_model_version
"""

from alembic import op
from sqlalchemy.orm import Session

from app import models
from app.rbac import ensure_default_roles

revision = "0007_rbac"
down_revision = "0006_embedding_model_version"
branch_labels = None
depends_on = None


TABLES = (
    models.Permission.__table__,
    models.Role.__table__,
    models.RolePermission.__table__,
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        table.create(bind=bind, checkfirst=True)
    with Session(bind=bind) as session:
        ensure_default_roles(session)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(TABLES):
        table.drop(bind=bind, checkfirst=True)
