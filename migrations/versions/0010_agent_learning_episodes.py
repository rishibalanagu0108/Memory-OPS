"""Add scoped immutable agent checkpoints and observable episodes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_agent_learning_episodes"
down_revision = "0009_vector_retrieval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_checkpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(255), nullable=False),
        sa.Column("environment", sa.String(255), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column(
            "observations",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "sequence", name="uq_agent_checkpoints_sequence"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "agent_id",
            "run_id",
            "id",
            name="uq_agent_checkpoints_episode_link",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_agent_checkpoints_sequence"),
        sa.CheckConstraint(
            "state IN ('active', 'waiting', 'completed', 'failed')",
            name="ck_agent_checkpoints_state",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(observations) = 'array'",
            name="ck_agent_checkpoints_observations",
        ),
    )
    op.create_index(
        "ix_agent_checkpoints_run",
        "agent_checkpoints",
        ["tenant_id", "workspace_id", "agent_id", "run_id", "sequence"],
    )

    op.create_table(
        "agent_episodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint_id", sa.Uuid()),
        sa.Column("task_type", sa.String(255), nullable=False),
        sa.Column("environment", sa.String(255), nullable=False),
        sa.Column("episode_type", sa.String(20), nullable=False),
        sa.Column("action", sa.String(255), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("outcome_status", sa.String(20), nullable=False),
        sa.Column("tool_name", sa.String(255)),
        sa.Column("tool_version", sa.String(255)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "agent_id", "run_id", "checkpoint_id"],
            [
                "agent_checkpoints.tenant_id",
                "agent_checkpoints.workspace_id",
                "agent_checkpoints.agent_id",
                "agent_checkpoints.run_id",
                "agent_checkpoints.id",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "episode_type IN ('action', 'tool_call', 'outcome', 'error')",
            name="ck_agent_episodes_type",
        ),
        sa.CheckConstraint(
            "outcome_status IN ('success', 'failure', 'partial')",
            name="ck_agent_episodes_outcome_status",
        ),
        sa.CheckConstraint(
            "(tool_name IS NULL) = (tool_version IS NULL)",
            name="ck_agent_episodes_tool_identity",
        ),
    )
    op.create_index(
        "ix_agent_episodes_run",
        "agent_episodes",
        ["tenant_id", "workspace_id", "agent_id", "run_id", "observed_at"],
    )

    for table in ("agent_checkpoints", "agent_episodes"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (
                tenant_id = NULLIF(
                    current_setting('memory_ops.tenant_id', true), ''
                )::uuid
            )
            WITH CHECK (
                tenant_id = NULLIF(
                    current_setting('memory_ops.tenant_id', true), ''
                )::uuid
            )
            """
        )

    op.execute(
        """
        CREATE FUNCTION reject_agent_learning_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'agent learning records are immutable';
        END;
        $$
        """
    )
    for table in ("agent_checkpoints", "agent_episodes"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_agent_learning_mutation()
            """
        )

    op.execute("REVOKE ALL ON agent_checkpoints, agent_episodes FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT ON agent_checkpoints, agent_episodes "
        "TO memory_ops_runtime"
    )


def downgrade() -> None:
    for table in ("agent_episodes", "agent_checkpoints"):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
    op.execute("DROP FUNCTION reject_agent_learning_mutation")
    op.drop_table("agent_episodes")
    op.drop_table("agent_checkpoints")
