"""Add PostgreSQL full-text indexing for canonical memory versions."""

from alembic import op

revision = "0008_full_text_retrieval"
down_revision = "0007_purge_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_memory_versions
        ADD COLUMN search_document tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english'::regconfig, original_statement)
        ) STORED
        """
    )
    op.execute(
        "CREATE INDEX ix_user_memory_versions_search_document "
        "ON user_memory_versions USING gin (search_document)"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_memory_versions_search_document",
        table_name="user_memory_versions",
    )
    op.drop_column("user_memory_versions", "search_document")
