"""Allow the connection owner to assume the restricted runtime role."""

from alembic import op

revision = "0003_runtime_role_membership"
down_revision = "0002_idempotency_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT memory_ops_runtime TO CURRENT_USER")


def downgrade() -> None:
    op.execute("REVOKE memory_ops_runtime FROM CURRENT_USER")
