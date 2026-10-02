"""Create tenant boundaries and enforce PostgreSQL row-level security."""

import sqlalchemy as sa
from alembic import op

revision = "0001_tenant_boundaries"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_workspaces_tenant_id", "workspaces", ["tenant_id"])

    op.execute(
        """
        DO $$ BEGIN
            CREATE ROLE memory_ops_runtime
                NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
        EXCEPTION WHEN duplicate_object THEN
            ALTER ROLE memory_ops_runtime
                NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
        END $$
        """
    )
    op.execute("REVOKE ALL ON tenants, workspaces FROM PUBLIC")
    op.execute("GRANT SELECT ON tenants TO memory_ops_runtime")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON workspaces TO memory_ops_runtime"
    )

    for table, tenant_column in (("tenants", "id"), ("workspaces", "tenant_id")):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (
                {tenant_column} = NULLIF(
                    current_setting('memory_ops.tenant_id', true), ''
                )::uuid
            )
            WITH CHECK (
                {tenant_column} = NULLIF(
                    current_setting('memory_ops.tenant_id', true), ''
                )::uuid
            )
            """
        )


def downgrade() -> None:
    op.drop_table("workspaces")
    op.drop_table("tenants")
    op.execute("DROP ROLE IF EXISTS memory_ops_runtime")
