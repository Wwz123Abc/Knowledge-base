"""Add user_role_assignments: grant an external identity (e.g. WeCom userid) a role.

Revision ID: 0008_user_role_assignments
Revises: 0007_rbac
"""

from alembic import op

from app import models

revision = "0008_user_role_assignments"
down_revision = "0007_rbac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    models.UserRoleAssignment.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    models.UserRoleAssignment.__table__.drop(bind=bind, checkfirst=True)
