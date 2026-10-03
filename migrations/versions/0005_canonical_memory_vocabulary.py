"""Align canonical memory vocabulary with the approved M1 contract."""

from alembic import op

revision = "0005_canonical_memory_vocabulary"
down_revision = "0004_user_memory_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_memory_versions DISABLE TRIGGER user_memory_versions_immutable")
    op.drop_constraint(
        "ck_user_memory_versions_sensitivity",
        "user_memory_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_user_memory_versions_origin", "user_memory_versions", type_="check"
    )
    op.execute(
        """
        UPDATE user_memory_versions
        SET sensitivity = CASE sensitivity
            WHEN 'public' THEN 'normal'
            WHEN 'internal' THEN 'normal'
            WHEN 'confidential' THEN 'sensitive'
            ELSE sensitivity
        END,
        origin = CASE origin
            WHEN 'user' THEN 'explicit'
            WHEN 'import' THEN 'extracted'
            WHEN 'agent' THEN 'derived'
            WHEN 'inferred' THEN 'derived'
            ELSE origin
        END
        """
    )
    op.create_check_constraint(
        "ck_user_memory_versions_sensitivity",
        "user_memory_versions",
        "sensitivity IN ('normal', 'sensitive', 'restricted')",
    )
    op.create_check_constraint(
        "ck_user_memory_versions_origin",
        "user_memory_versions",
        "origin IN ('explicit', 'extracted', 'derived')",
    )
    op.execute("ALTER TABLE user_memory_versions ENABLE TRIGGER user_memory_versions_immutable")


def downgrade() -> None:
    op.execute("ALTER TABLE user_memory_versions DISABLE TRIGGER user_memory_versions_immutable")
    op.drop_constraint(
        "ck_user_memory_versions_sensitivity",
        "user_memory_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_user_memory_versions_origin", "user_memory_versions", type_="check"
    )
    op.execute(
        """
        UPDATE user_memory_versions
        SET sensitivity = CASE sensitivity
            WHEN 'normal' THEN 'internal'
            WHEN 'sensitive' THEN 'confidential'
            ELSE sensitivity
        END,
        origin = CASE origin
            WHEN 'explicit' THEN 'user'
            WHEN 'extracted' THEN 'import'
            WHEN 'derived' THEN 'inferred'
            ELSE origin
        END
        """
    )
    op.create_check_constraint(
        "ck_user_memory_versions_sensitivity",
        "user_memory_versions",
        "sensitivity IN ('public', 'internal', 'confidential', 'restricted')",
    )
    op.create_check_constraint(
        "ck_user_memory_versions_origin",
        "user_memory_versions",
        "origin IN ('user', 'agent', 'import', 'inferred')",
    )
    op.execute("ALTER TABLE user_memory_versions ENABLE TRIGGER user_memory_versions_immutable")
