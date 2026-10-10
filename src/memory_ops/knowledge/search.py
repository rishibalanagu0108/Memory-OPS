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
            incomplete = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM knowledge_documents d
                        JOIN knowledge_document_versions v
                          ON v.tenant_id = d.tenant_id
                         AND v.document_id = d.id
                         AND v.id = d.current_version_id
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
                          AND d.lifecycle = 'active'
                          AND d.ingestion_status != 'ready'
                          AND v.publication_status = 'published'
                          AND v.effective_from <= now()
                          AND (v.effective_to IS NULL OR v.effective_to > now())
                    )
                    """
                ),
                {
                    "workspace_id": scope.workspace_id,
                    "principal_id": principal_id,
                },
            ).scalar_one()
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
                    )
                    SELECT c.id AS chunk_id, c.content, c.content_hash,
                           c.locator_kind, c.locator_path,
                           c.start_line, c.end_line, c.structure_path,
                           d.id AS document_id, d.source_id, d.title,
                           v.id AS document_version_id,
                           v.content_hash AS document_content_hash,
                           a.policy_version AS access_policy_version,
                           ts_rank_cd(
                               to_tsvector('english', c.content), q.value
                           )::float AS score
                    FROM knowledge_document_chunks c
                    JOIN knowledge_document_versions v
                      ON v.tenant_id = c.tenant_id
                     AND v.document_id = c.document_id
                     AND v.id = c.document_version_id
                    JOIN knowledge_documents d
                      ON d.tenant_id = v.tenant_id
                     AND d.id = v.document_id
                    JOIN knowledge_document_acl_revisions a
                      ON a.tenant_id = d.tenant_id
                     AND a.document_id = d.id
                     AND a.id = d.current_acl_revision_id
                    JOIN knowledge_document_acl_grants g
                      ON g.tenant_id = a.tenant_id
                     AND g.document_id = a.document_id
                     AND g.acl_revision_id = a.id
                    CROSS JOIN q
                    WHERE d.workspace_id = :workspace_id
                      AND g.principal_id = :principal_id
                      AND d.lifecycle = 'active'
                      AND d.ingestion_status = 'ready'
                      AND d.current_version_id = v.id
                      AND v.publication_status = 'published'
                      AND v.effective_from <= now()
                      AND (v.effective_to IS NULL OR v.effective_to > now())
                      AND c.index_generation = :index_generation
                      AND to_tsvector('english', c.content) @@ q.value
                    ORDER BY score DESC, d.id, c.ordinal
                    LIMIT :limit
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
            stale = connection.execute(
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
                    )
                    SELECT EXISTS (
                        SELECT 1
                        FROM knowledge_document_chunks c
                        JOIN knowledge_documents d
                          ON d.tenant_id = c.tenant_id
                         AND d.id = c.document_id
                        JOIN knowledge_document_versions v
                          ON v.tenant_id = c.tenant_id
                         AND v.document_id = c.document_id
                         AND v.id = c.document_version_id
                        JOIN knowledge_document_acl_revisions a
                          ON a.tenant_id = d.tenant_id
                         AND a.document_id = d.id
                         AND a.id = d.current_acl_revision_id
                        JOIN knowledge_document_acl_grants g
                          ON g.tenant_id = a.tenant_id
                         AND g.document_id = a.document_id
                         AND g.acl_revision_id = a.id
                        CROSS JOIN q
                        WHERE d.workspace_id = :workspace_id
                          AND g.principal_id = :principal_id
                          AND to_tsvector('english', c.content) @@ q.value
                          AND (
                              numnode(q.value) = 1
                              OR ts_rank_cd(
                                  to_tsvector('english', c.content), q.value
                              ) >= 0.2
                          )
                          AND (
                              d.lifecycle != 'active'
                              OR d.current_version_id != v.id
                              OR v.publication_status != 'published'
                              OR v.effective_from > now()
                              OR (v.effective_to IS NOT NULL AND v.effective_to <= now())
                          )
                    )
                    """
                ),
                {
                    "workspace_id": scope.workspace_id,
                    "principal_id": principal_id,
                    "query": normalized_query,
                },
            ).scalar_one()

        passages = tuple(self._passage(row) for row in rows)
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
