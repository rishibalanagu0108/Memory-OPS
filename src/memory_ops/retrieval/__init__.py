"""Authorized candidate retrieval over current canonical memories."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import text

from memory_ops.persistence import TenantDatabase
from memory_ops.user_memory import MemoryScope, SemanticType
from memory_ops.retrieval.embeddings import (
    EmbeddingModel,
    EmbeddingPolicy,
    EmbeddingProvider,
    HashEmbeddingProvider,
    vector_literal,
)


Channel = Literal["exact", "filtered", "keyword", "vector"]


@dataclass(frozen=True)
class RetrievalCandidate:
    memory_id: UUID
    version_id: UUID
    statement: str
    semantic_type: SemanticType
    purpose: str
    score: float
    channel: Channel


class RetrievalService:
    def __init__(self, database: TenantDatabase) -> None:
        self.database = database

    def exact(
        self,
        scope: MemoryScope,
        memory_id: UUID,
        *,
        purpose: str,
        access_scopes: tuple[str, ...] = (),
    ) -> RetrievalCandidate | None:
        self._validate(purpose, access_scopes, 1)
        with self.database.transaction(scope.tenant_id) as connection:
            row = connection.execute(
                text(
                    f"""
                    {self._select("1.0", "exact")}
                    WHERE m.id = :memory_id
                      AND {self._authorized_current()}
                    """
                ),
                self._parameters(scope, purpose, access_scopes)
                | {"memory_id": memory_id},
            ).mappings().one_or_none()
        return self._candidate(row) if row is not None else None

    def filtered(
        self,
        scope: MemoryScope,
        *,
        purpose: str,
        semantic_types: tuple[SemanticType, ...] = (),
        access_scopes: tuple[str, ...] = (),
        limit: int = 20,
    ) -> tuple[RetrievalCandidate, ...]:
        self._validate(purpose, access_scopes, limit)
        with self.database.transaction(scope.tenant_id) as connection:
            rows = connection.execute(
                text(
                    f"""
                    {self._select("1.0", "filtered")}
                    WHERE {self._authorized_current()}
                      AND (
                          cardinality(CAST(:semantic_types AS text[])) = 0
                          OR m.semantic_type = ANY(CAST(:semantic_types AS text[]))
                      )
                    ORDER BY v.recorded_at DESC, m.id
                    LIMIT :limit
                    """
                ),
                self._parameters(scope, purpose, access_scopes)
                | {"semantic_types": list(semantic_types), "limit": limit},
            ).mappings()
            return tuple(self._candidate(row) for row in rows)

    def keyword(
        self,
        scope: MemoryScope,
        query: str,
        *,
        purpose: str,
        semantic_types: tuple[SemanticType, ...] = (),
        access_scopes: tuple[str, ...] = (),
        limit: int = 20,
    ) -> tuple[RetrievalCandidate, ...]:
        self._validate(purpose, access_scopes, limit)
        if not query.strip():
            return ()
        if len(query) > 10_000:
            raise ValueError("query must contain at most 10000 characters")
        with self.database.transaction(scope.tenant_id) as connection:
            rows = connection.execute(
                text(
                    f"""
                    {self._select("ts_rank_cd(v.search_document, q.query)", "keyword")}
                    CROSS JOIN plainto_tsquery('english', :query) AS q(query)
                    WHERE {self._authorized_current()}
                      AND v.search_document @@ q.query
                      AND (
                          cardinality(CAST(:semantic_types AS text[])) = 0
                          OR m.semantic_type = ANY(CAST(:semantic_types AS text[]))
                      )
                    ORDER BY score DESC, v.recorded_at DESC, m.id
                    LIMIT :limit
                    """
                ),
                self._parameters(scope, purpose, access_scopes)
                | {
                    "query": query,
                    "semantic_types": list(semantic_types),
                    "limit": limit,
                },
            ).mappings()
            return tuple(self._candidate(row) for row in rows)

    def vector(
        self,
        scope: MemoryScope,
        embedding: tuple[float, ...],
        *,
        purpose: str,
        model: EmbeddingModel,
        index_generation: str,
        access_scopes: tuple[str, ...] = (),
        limit: int = 20,
    ) -> tuple[RetrievalCandidate, ...]:
        self._validate(purpose, access_scopes, limit)
        if not index_generation.strip() or len(index_generation) > 255:
            raise ValueError("index generation must contain 1 to 255 characters")
        encoded = vector_literal(embedding, model.dimensions)
        with self.database.transaction(scope.tenant_id) as connection:
            rows = connection.execute(
                text(
                    f"""
                    {self._select("1 - (e.embedding <=> CAST(:embedding AS vector))", "vector")}
                    JOIN user_memory_embeddings e
                      ON e.memory_id = m.id
                     AND e.canonical_version_id = v.id
                    WHERE {self._authorized_current()}
                      AND e.index_generation = :index_generation
                      AND e.provider = :provider
                      AND e.model_name = :model_name
                      AND e.model_version = :model_version
                    ORDER BY e.embedding <=> CAST(:embedding AS vector), m.id
                    LIMIT :limit
                    """
                ),
                self._parameters(scope, purpose, access_scopes)
                | {
                    "embedding": encoded,
                    "index_generation": index_generation,
                    "provider": model.provider,
                    "model_name": model.name,
                    "model_version": model.version,
                    "limit": limit,
                },
            ).mappings()
            return tuple(self._candidate(row) for row in rows)

    def hybrid(
        self,
        scope: MemoryScope,
        query: str,
        embedding: tuple[float, ...],
        *,
        purpose: str,
        model: EmbeddingModel,
        index_generation: str,
        promotion,
        access_scopes: tuple[str, ...] = (),
        candidate_limit: int = 40,
        limit: int = 20,
        rank_constant: int = 60,
    ) -> tuple[RetrievalCandidate, ...]:
        from memory_ops.retrieval.rank_fusion import (
            HybridNotPromoted,
            reciprocal_rank_fusion,
        )

        if not promotion.release_enabled:
            raise HybridNotPromoted(", ".join(promotion.reasons))
        keyword = self.keyword(
            scope,
            query,
            purpose=purpose,
            access_scopes=access_scopes,
            limit=candidate_limit,
        )
        vector = self.vector(
            scope,
            embedding,
            purpose=purpose,
            model=model,
            index_generation=index_generation,
            access_scopes=access_scopes,
            limit=candidate_limit,
        )
        return reciprocal_rank_fusion(
            (keyword, vector),
            rank_constant=rank_constant,
            limit=limit,
        )

    @staticmethod
    def _select(score: str, channel: Channel) -> str:
        return f"""
            SELECT m.id AS memory_id, v.id AS version_id,
                   v.original_statement AS statement, m.semantic_type,
                   v.purpose, ({score})::float AS score, '{channel}' AS channel
            FROM user_memories m
            JOIN user_memory_versions v ON v.id = m.current_version_id
        """

    @staticmethod
    def _authorized_current() -> str:
        return """
            m.workspace_id = :workspace_id
            AND m.subject_id = :subject_id
            AND (m.agent_id IS NULL OR m.agent_id = CAST(:agent_id AS uuid))
            AND m.lifecycle = 'active'
            AND v.id = m.current_version_id
            AND v.purpose = :purpose
            AND v.valid_from <= now()
            AND (v.valid_to IS NULL OR v.valid_to > now())
            AND (v.retention_until IS NULL OR v.retention_until > now())
            AND (
                v.access_scope = '[]'::jsonb
                OR v.access_scope ?| CAST(:access_scopes AS text[])
            )
        """

    @staticmethod
    def _parameters(
        scope: MemoryScope, purpose: str, access_scopes: tuple[str, ...]
    ) -> dict[str, object]:
        return {
            "workspace_id": scope.workspace_id,
            "subject_id": scope.subject_id,
            "agent_id": scope.agent_id,
            "purpose": purpose,
            "access_scopes": list(access_scopes),
        }

    @staticmethod
    def _validate(purpose: str, access_scopes: tuple[str, ...], limit: int) -> None:
        if not purpose.strip() or len(purpose) > 255:
            raise ValueError("purpose must contain 1 to 255 characters")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if any(not value.strip() or len(value) > 255 for value in access_scopes):
            raise ValueError("access scopes must contain 1 to 255 characters")

    @staticmethod
    def _candidate(row) -> RetrievalCandidate:
        return RetrievalCandidate(
            memory_id=row.memory_id,
            version_id=row.version_id,
            statement=row.statement,
            semantic_type=row.semantic_type,
            purpose=row.purpose,
            score=row.score,
            channel=row.channel,
        )


__all__ = [
    "EmbeddingModel",
    "EmbeddingPolicy",
    "EmbeddingProvider",
    "HashEmbeddingProvider",
    "HybridNotPromoted",
    "PromotionCriteria",
    "PromotionDecision",
    "RetrievalCandidate",
    "RetrievalMeasurements",
    "RetrievalService",
    "evaluate_rrf_promotion",
    "reciprocal_rank_fusion",
]


from memory_ops.retrieval.rank_fusion import (  # noqa: E402
    HybridNotPromoted,
    PromotionCriteria,
    PromotionDecision,
    RetrievalMeasurements,
    evaluate_rrf_promotion,
    reciprocal_rank_fusion,
)
