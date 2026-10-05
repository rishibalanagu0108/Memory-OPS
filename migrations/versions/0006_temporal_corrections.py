"""Add immutable correction links to canonical memory versions."""

import sqlalchemy as sa
from alembic import op

revision = "0006_temporal_corrections"
down_revision = "0005_canonical_memory_vocabulary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_memory_versions",
        sa.Column("supersedes_version_id", sa.Uuid()),
    )
    op.add_column(
        "user_memory_versions",
        sa.Column(
            "change_kind",
            sa.String(20),
            nullable=False,
            server_default="initial",
        ),
    )
    op.create_foreign_key(
        "fk_user_memory_versions_supersedes",
        "user_memory_versions",
        "user_memory_versions",
        ["tenant_id", "memory_id", "supersedes_version_id"],
        ["tenant_id", "memory_id", "id"],
    )
    op.create_unique_constraint(
        "uq_user_memory_versions_successor",
        "user_memory_versions",
        ["tenant_id", "memory_id", "supersedes_version_id"],
    )
    op.create_check_constraint(
        "ck_user_memory_versions_change_kind",
        "user_memory_versions",
        "change_kind IN ('initial', 'correction', 'temporal_change')",
    )
    op.create_check_constraint(
        "ck_user_memory_versions_predecessor",
        "user_memory_versions",
        "(change_kind = 'initial' AND supersedes_version_id IS NULL) OR "
        "(change_kind <> 'initial' AND supersedes_version_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_user_memory_versions_predecessor",
        "user_memory_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_user_memory_versions_change_kind",
        "user_memory_versions",
        type_="check",
    )
    op.drop_constraint(
        "uq_user_memory_versions_successor",
        "user_memory_versions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_user_memory_versions_supersedes",
        "user_memory_versions",
        type_="foreignkey",
    )
    op.drop_column("user_memory_versions", "change_kind")
    op.drop_column("user_memory_versions", "supersedes_version_id")
