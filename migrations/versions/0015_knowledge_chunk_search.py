"""Add indexed full-text search to knowledge-document chunks."""

from alembic import op


revision = "0015_knowledge_chunk_search"
down_revision = "0014_knowledge_acl_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE knowledge_document_chunks
        ADD COLUMN search_document tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english'::regconfig, content)
        ) STORED
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_document_chunks_search_document
        ON knowledge_document_chunks USING gin (search_document)
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_document_chunks_search_document",
        table_name="knowledge_document_chunks",
    )
    op.drop_column("knowledge_document_chunks", "search_document")
