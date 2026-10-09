"""Add evidence-linked candidates and immutable lesson versions."""

import sqlalchemy as sa
from alembic import op

revision = "0011_agent_lesson_versions"
down_revision = "0010_agent_learning_episodes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_agent_episodes_lesson_evidence",
        "agent_episodes",
        [
            "tenant_id",
            "workspace_id",
            "agent_id",
            "task_type",
            "environment",
            "tool_name",
            "tool_version",
            "run_id",
            "id",
        ],
    )
    op.create_table(
        "agent_lesson_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(255), nullable=False),
        sa.Column("environment", sa.String(255), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("tool_version", sa.String(255), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("procedure", sa.Text(), nullable=False),
        sa.Column("generator_provider", sa.String(255), nullable=False),
        sa.Column("generator_name", sa.String(255), nullable=False),
        sa.Column("generator_version", sa.String(255), nullable=False),
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
            "tenant_id",
            "workspace_id",
            "agent_id",
            "task_type",
            "environment",
            "tool_name",
            "tool_version",
            "id",
            name="uq_agent_lesson_candidates_scope",
        ),
    )
    op.create_table(
        "agent_lesson_candidate_evidence",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(255), nullable=False),
        sa.Column("environment", sa.String(255), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("tool_version", sa.String(255), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_run_id", sa.Uuid(), nullable=False),
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "workspace_id",
                "agent_id",
                "task_type",
                "environment",
                "tool_name",
                "tool_version",
                "candidate_id",
            ],
            [
                "agent_lesson_candidates.tenant_id",
                "agent_lesson_candidates.workspace_id",
                "agent_lesson_candidates.agent_id",
                "agent_lesson_candidates.task_type",
                "agent_lesson_candidates.environment",
                "agent_lesson_candidates.tool_name",
                "agent_lesson_candidates.tool_version",
                "agent_lesson_candidates.id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "workspace_id",
                "agent_id",
                "task_type",
                "environment",
                "tool_name",
                "tool_version",
                "evidence_run_id",
                "episode_id",
            ],
            [
                "agent_episodes.tenant_id",
                "agent_episodes.workspace_id",
                "agent_episodes.agent_id",
                "agent_episodes.task_type",
                "agent_episodes.environment",
                "agent_episodes.tool_name",
                "agent_episodes.tool_version",
                "agent_episodes.run_id",
                "agent_episodes.id",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "candidate_id", "episode_id"),
    )
    op.create_table(
        "agent_lesson_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(255), nullable=False),
        sa.Column("environment", sa.String(255), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("tool_version", sa.String(255), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("procedure", sa.Text(), nullable=False),
        sa.Column("evaluation_reference", sa.String(255), nullable=False),
        sa.Column("evaluation_contract_version", sa.String(255), nullable=False),
        sa.Column("evaluation_dataset_version", sa.String(255), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("generator_provider", sa.String(255), nullable=False),
        sa.Column("generator_name", sa.String(255), nullable=False),
        sa.Column("generator_version", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "workspace_id",
                "agent_id",
                "task_type",
                "environment",
                "tool_name",
                "tool_version",
                "candidate_id",
            ],
            [
                "agent_lesson_candidates.tenant_id",
                "agent_lesson_candidates.workspace_id",
                "agent_lesson_candidates.agent_id",
                "agent_lesson_candidates.task_type",
                "agent_lesson_candidates.environment",
                "agent_lesson_candidates.tool_name",
                "agent_lesson_candidates.tool_version",
                "agent_lesson_candidates.id",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "version_number",
            name="uq_agent_lesson_versions_number",
        ),
        sa.CheckConstraint(
            "version_number >= 1", name="ck_agent_lesson_versions_number"
        ),
        sa.CheckConstraint(
            "stage IN ('evaluated', 'promoted')",
            name="ck_agent_lesson_versions_stage",
        ),
        sa.CheckConstraint(
            "source_hash ~ '^[0-9a-f]{64}$'",
            name="ck_agent_lesson_versions_source_hash",
        ),
    )
    op.create_index(
        "ix_agent_lesson_versions_scope",
        "agent_lesson_versions",
        [
            "tenant_id",
            "workspace_id",
            "agent_id",
            "task_type",
            "environment",
            "tool_name",
            "tool_version",
            "stage",
        ],
    )

    for table in (
        "agent_lesson_candidates",
        "agent_lesson_candidate_evidence",
        "agent_lesson_versions",
    ):
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
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_agent_learning_mutation()
            """
        )

    op.execute(
        "REVOKE ALL ON agent_lesson_candidates, "
        "agent_lesson_candidate_evidence, agent_lesson_versions FROM PUBLIC"
    )
    op.execute(
        "GRANT SELECT, INSERT ON agent_lesson_candidates, "
        "agent_lesson_candidate_evidence, agent_lesson_versions "
        "TO memory_ops_runtime"
    )


def downgrade() -> None:
    op.drop_table("agent_lesson_versions")
    op.drop_table("agent_lesson_candidate_evidence")
    op.drop_table("agent_lesson_candidates")
    op.drop_constraint(
        "uq_agent_episodes_lesson_evidence", "agent_episodes", type_="unique"
    )
