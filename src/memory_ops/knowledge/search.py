"""Authorized retrieval over current knowledge-document chunk projections."""

from dataclasses import dataclass
import re
from typing import Literal
from uuid import UUID

from sqlalchemy import text

from memory_ops.knowledge import KnowledgeScope
from memory_ops.persistence import TenantDatabase


_SUSPICIOUS_INSTRUCTION = re.compile(
    r"\b(?:ignore|disregard|override)\b.{0,80}"
    r"\b(?:instruction|policy|access control)\b|"
    r"\breveal\b.{0,40}\b(?:secret|credential|token)\b",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class KnowledgeCitation:
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    source_id: str
    title: str
    document_content_hash: str
    chunk_content_hash: str
    access_policy_version: str
    locator_kind: str
    locator_path: str
    start_line: int
    end_line: int
    structure_path: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgePassage:
    content: str
    score: float
    trust: Literal["untrusted"]
    citation: KnowledgeCitation


@dataclass(frozen=True)
class KnowledgeSearchResult:
    passages: tuple[KnowledgePassage, ...]
    warnings: tuple[str, ...]
    partial: bool
    abstained: bool
    retrieval_channel: Literal["keyword"] = "keyword"


class KnowledgeSearchService:
    def __init__(
        self,
        database: TenantDatabase,
        index_generation: str = "knowledge-v1",
    ) -> None:
        if not index_generation.strip() or len(index_generation) > 255:
            raise ValueError("index generation must contain 1 to 255 characters")
        self.database = database
        self.index_generation = index_generation

    def search(
        self,
        scope: KnowledgeScope,
        principal_id: UUID,
        query: str,
        *,
        limit: int = 10,
    ) -> KnowledgeSearchResult:
        normalized_query = query.strip()
        if not normalized_query or len(query) > 10_000:
            raise ValueError("query must contain 1 to 10000 characters")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        with self.database.transaction(scope.tenant_id) as connection:
            rows = connection.execute(
                text(
                    """
                    WITH q AS (
                        SELECT to_tsquery(
                            'english',
                            array_to_string(
                                tsvector_to_array(
                                    to_tsvector('english', :query)
                                ),
                                ' | '
                            )
                        ) AS value
                    ),
                    authorized_documents AS MATERIALIZED (
                        SELECT d.id AS document_id, d.source_id, d.title,
                               d.lifecycle, d.ingestion_status,
                               d.current_version_id,
                               v.id AS document_version_id,
                               v.content_hash AS document_content_hash,
                               v.publication_status, v.effective_from,
                               v.effective_to,
                               a.policy_version AS access_policy_version
                        FROM knowledge_documents d
                        JOIN knowledge_document_versions v
                          ON v.tenant_id = d.tenant_id
                         AND v.document_id = d.id
                        JOIN knowledge_document_acl_revisions a
                          ON a.tenant_id = d.tenant_id
                         AND a.document_id = d.id
                         AND a.id = d.current_acl_revision_id
                        JOIN knowledge_document_acl_grants g
                          ON g.tenant_id = a.tenant_id
                         AND g.document_id = a.document_id
                         AND g.acl_revision_id = a.id
                        WHERE d.workspace_id = :workspace_id
                          AND g.principal_id = :principal_id
                    ),
                    matches AS MATERIALIZED (
                        SELECT c.id AS chunk_id, c.content, c.content_hash,
                               c.locator_kind, c.locator_path,
                               c.start_line, c.end_line, c.structure_path,
                               c.index_generation, c.ordinal,
                               d.*,
                               ts_rank_cd(c.search_document, q.value)::float AS score
                        FROM knowledge_document_chunks c
                        JOIN authorized_documents d
                          ON d.document_id = c.document_id
                         AND d.document_version_id = c.document_version_id
                        CROSS JOIN q
                        WHERE c.search_document @@ q.value
                    ),
                    current_results AS (
                        SELECT *
                        FROM matches
                        WHERE lifecycle = 'active'
                          AND ingestion_status = 'ready'
                          AND current_version_id = document_version_id
                          AND publication_status = 'published'
                          AND effective_from <= now()
                          AND (effective_to IS NULL OR effective_to > now())
                          AND index_generation = :index_generation
                        ORDER BY score DESC, document_id, ordinal
                        LIMIT :limit
                    ),
                    flags AS (
                        SELECT EXISTS (
                                   SELECT 1
                                   FROM authorized_documents
                                   WHERE lifecycle = 'active'
                                     AND ingestion_status != 'ready'
                                     AND current_version_id = document_version_id
                                     AND publication_status = 'published'
                                     AND effective_from <= now()
                                     AND (effective_to IS NULL OR effective_to > now())
                               ) AS incomplete,
                               EXISTS (
                                   SELECT 1
                                   FROM matches, q
                                   WHERE (numnode(q.value) = 1 OR score >= 0.2)
                                     AND (
                                         lifecycle != 'active'
                                         OR current_version_id != document_version_id
                                         OR publication_status != 'published'
                                         OR effective_from > now()
                                         OR (effective_to IS NOT NULL AND effective_to <= now())
                                     )
                               ) AS stale
                    )
                    SELECT r.*, flags.incomplete, flags.stale
                    FROM flags
                    LEFT JOIN LATERAL (
                        SELECT * FROM current_results
                    ) r ON true
                    ORDER BY r.score DESC NULLS LAST, r.document_id, r.ordinal
                    """
                ),
                {
                    "workspace_id": scope.workspace_id,
                    "principal_id": principal_id,
                    "query": normalized_query,
                    "index_generation": self.index_generation,
                    "limit": limit,
                },
            ).mappings().all()

        incomplete = bool(rows[0].incomplete)
        stale = bool(rows[0].stale)
        passages = tuple(self._passage(row) for row in rows if row.chunk_id is not None)
        warnings: list[str] = []
        if incomplete:
            warnings.append("knowledge_index_incomplete")
        if stale:
            warnings.append("stale_source_filtered")
        if not passages:
            warnings.append("insufficient_authorized_evidence")
        if any(_SUSPICIOUS_INSTRUCTION.search(passage.content) for passage in passages):
            warnings.append("untrusted_source_content")
        if self._has_numeric_conflict(passages):
            warnings.append("unresolved_conflict")
        return KnowledgeSearchResult(
            passages,
            tuple(warnings),
            partial=bool(incomplete),
            abstained=not passages,
        )

    @staticmethod
    def _has_numeric_conflict(passages: tuple[KnowledgePassage, ...]) -> bool:
        numbers_by_document: dict[UUID, frozenset[str]] = {}
        for passage in passages:
            numbers = frozenset(re.findall(r"\b\d+(?:\.\d+)?\b", passage.content))
            if numbers:
                numbers_by_document[passage.citation.document_id] = numbers
        return (
            len(numbers_by_document) > 1
            and len(set(numbers_by_document.values())) > 1
        )

    @staticmethod
    def _passage(row: object) -> KnowledgePassage:
        return KnowledgePassage(
            content=row.content,
            score=row.score,
            trust="untrusted",
            citation=KnowledgeCitation(
                document_id=row.document_id,
                document_version_id=row.document_version_id,
                chunk_id=row.chunk_id,
                source_id=row.source_id,
                title=row.title,
                document_content_hash=row.document_content_hash,
                chunk_content_hash=row.content_hash,
                access_policy_version=row.access_policy_version,
                locator_kind=row.locator_kind,
                locator_path=row.locator_path,
                start_line=row.start_line,
                end_line=row.end_line,
                structure_path=tuple(row.structure_path),
            ),
        )
