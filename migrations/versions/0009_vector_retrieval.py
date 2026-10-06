"""Add policy-traceable vector artifacts and Lakebase ANN search."""

from alembic import op

revision = "0009_vector_retrieval"
down_revision = "0008_full_text_retrieval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE")
    op.execute(
        """
        CREATE TABLE user_memory_embeddings (
            tenant_id uuid NOT NULL,
            memory_id uuid NOT NULL,
            canonical_version_id uuid NOT NULL,
            index_generation varchar(255) NOT NULL,
            provider varchar(100) NOT NULL,
            model_name varchar(255) NOT NULL,
            model_version varchar(255) NOT NULL,
            embedding vector(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (
                tenant_id, canonical_version_id, index_generation,
                provider, model_name, model_version
            ),
            CONSTRAINT fk_user_memory_embeddings_version
                FOREIGN KEY (tenant_id, memory_id, canonical_version_id)
                REFERENCES user_memory_versions (tenant_id, memory_id, id)
                ON DELETE CASCADE,
            CONSTRAINT ck_user_memory_embeddings_generation
                CHECK (length(btrim(index_generation)) > 0),
            CONSTRAINT ck_user_memory_embeddings_provider
                CHECK (length(btrim(provider)) > 0),
            CONSTRAINT ck_user_memory_embeddings_model
                CHECK (length(btrim(model_name)) > 0),
            CONSTRAINT ck_user_memory_embeddings_model_version
                CHECK (length(btrim(model_version)) > 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_user_memory_embeddings_ann
        ON user_memory_embeddings
        USING lakebase_ann (embedding vector_cosine_ops)
        """
    )
    op.execute(
        "CREATE INDEX ix_user_memory_embeddings_identity "
        "ON user_memory_embeddings (tenant_id, memory_id, canonical_version_id)"
    )
    op.execute("ALTER TABLE user_memory_embeddings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_memory_embeddings FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY user_memory_embeddings_tenant_isolation
        ON user_memory_embeddings
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
        "GRANT SELECT, INSERT, DELETE ON user_memory_embeddings "
        "TO memory_ops_runtime"
    )


def downgrade() -> None:
    op.drop_table("user_memory_embeddings")
