"""Canonical hydration and token-budgeted user-memory context."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from memory_ops.agent_learning import LessonScope, LessonUse
from memory_ops.agent_learning.promotion import (
    LessonPromotionNotFound,
    LessonPromotionRegistry,
)
from memory_ops.knowledge.search import KnowledgeSearchResult
from memory_ops.persistence import TenantDatabase
from memory_ops.retrieval import HashEmbeddingProvider, RetrievalCandidate, RetrievalService
from memory_ops.retrieval.embeddings import EmbeddingProvider
from memory_ops.user_memory import EvidenceReference, MemoryNotFound, MemoryScope, UserMemoryService


@dataclass(frozen=True)
class ContextItem:
    domain: str
    memory_id: UUID
    version_id: UUID
    semantic_type: str
    content: str
    valid_from: datetime
    valid_to: datetime | None
    provenance: tuple[EvidenceReference, ...]
    tokens: int
    critical: bool


@dataclass(frozen=True)
class ContextSection:
    domain: str
    items: tuple[ContextItem, ...]


@dataclass(frozen=True)
class ContextResult:
    sections: tuple[ContextSection, ...]
    warnings: tuple[str, ...]
    partial: bool
    abstained: bool
    used_tokens: int
    token_budget: int


@dataclass(frozen=True)
class DomainRouteStatus:
    domain: str
    authorized: bool
    available: bool
    item_count: int


@dataclass(frozen=True)
class CrossDomainRetrieval:
    user_memory: ContextResult | None
    agent_learning: tuple[LessonUse, ...] | None
    organizational_knowledge: KnowledgeSearchResult | None
    domains: tuple[DomainRouteStatus, ...]
    warnings: tuple[str, ...]
    partial: bool


class UserContextService:
    def __init__(
        self,
        database: TenantDatabase,
        *,
        retrieval: RetrievalService | None = None,
        embedder: EmbeddingProvider | None = None,
        index_generation: str = "generation-1",
        min_vector_score: float = 0.25,
    ) -> None:
        self.database = database
        self.retrieval = retrieval or RetrievalService(database)
        self.embedder = embedder or HashEmbeddingProvider()
        self.index_generation = index_generation
        self.min_vector_score = min_vector_score

    def build(
        self,
        scope: MemoryScope,
        query: str,
        *,
        purpose: str,
        token_budget: int,
        access_scopes: tuple[str, ...] = (),
    ) -> ContextResult:
        if not query.strip() or len(query) > 10_000:
            raise ValueError("query must contain 1 to 10000 characters")
        warnings: list[str] = []
        try:
            candidates = self.retrieval.keyword(
                scope,
                query,
                purpose=purpose,
                access_scopes=access_scopes,
            )
        except SQLAlchemyError:
            candidates = ()
            warnings.append("keyword_unavailable")

        if not candidates:
            try:
                vector_candidates = self.retrieval.vector(
                    scope,
                    self.embedder.embed(query),
                    purpose=purpose,
                    model=self.embedder.metadata,
                    index_generation=self.index_generation,
                    access_scopes=access_scopes,
                )
                candidates = tuple(
                    candidate
                    for candidate in vector_candidates
                    if candidate.score >= self.min_vector_score
                )
            except SQLAlchemyError:
                candidates = ()
                warnings.append("vector_unavailable")
        return self.assemble(
            scope,
            candidates,
            purpose=purpose,
            token_budget=token_budget,
            access_scopes=access_scopes,
            warnings=tuple(warnings),
        )

    def assemble(
        self,
        scope: MemoryScope,
        candidates: tuple[RetrievalCandidate, ...],
        *,
        purpose: str,
        token_budget: int,
        access_scopes: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> ContextResult:
        if token_budget < 1:
            raise ValueError("token budget must be positive")
        now = datetime.now(UTC)
        hydrated: list[ContextItem] = []
        seen: set[UUID] = set()
        stale_filtered = False
        for candidate in candidates:
            if candidate.memory_id in seen:
                continue
            try:
                version = UserMemoryService(self.database, "context-read").current_version(
                    scope, candidate.memory_id
                )
            except MemoryNotFound:
                stale_filtered = True
                continue
            governance = version.governance
            authorized = (
                version.id == candidate.version_id
                and governance.purpose == purpose
                and version.valid_from <= now
                and (version.valid_to is None or version.valid_to > now)
                and (
                    governance.retention_until is None
                    or governance.retention_until > now
                )
                and (
                    not governance.access_scope
                    or bool(set(governance.access_scope) & set(access_scopes))
                )
            )
            if not authorized:
                stale_filtered = True
                continue
            seen.add(candidate.memory_id)
            tokens = max(1, len(version.original_statement.split()))
            hydrated.append(
                ContextItem(
                    domain="user_memory",
                    memory_id=version.memory_id,
                    version_id=version.id,
                    semantic_type=version.semantic_type,
                    content=version.original_statement,
                    valid_from=version.valid_from,
                    valid_to=version.valid_to,
                    provenance=version.evidence,
                    tokens=tokens,
                    critical=version.semantic_type == "constraint",
                )
            )

        output_warnings = list(warnings)
        if stale_filtered:
            output_warnings.append("stale_or_unauthorized_candidates_filtered")
        critical = [item for item in hydrated if item.critical]
        if sum(item.tokens for item in critical) > token_budget:
            output_warnings.append("critical_constraints_exceed_budget")
            return self._result((), output_warnings, token_budget, abstained=True)

        selected: list[ContextItem] = []
        used = 0
        for item in critical + [item for item in hydrated if not item.critical]:
            if used + item.tokens <= token_budget:
                selected.append(item)
                used += item.tokens
            else:
                output_warnings.append("token_budget_exhausted")
        if not selected:
            output_warnings.append("insufficient_evidence")
        return self._result(
            tuple(selected),
            output_warnings,
            token_budget,
            abstained=not selected,
        )

    @staticmethod
    def _result(
        items: tuple[ContextItem, ...],
        warnings: list[str],
        token_budget: int,
        *,
        abstained: bool,
    ) -> ContextResult:
        unique_warnings = tuple(dict.fromkeys(warnings))
        return ContextResult(
            sections=(ContextSection("user_memory", items),),
            warnings=unique_warnings,
            partial=bool(unique_warnings),
            abstained=abstained,
            used_tokens=sum(item.tokens for item in items),
            token_budget=token_budget,
        )


def select_agent_lessons(
    registry: LessonPromotionRegistry,
    scope: LessonScope,
    candidate_ids: tuple[UUID, ...],
    canary_key: str,
) -> tuple[LessonUse, ...]:
    selected: list[LessonUse] = []
    for candidate_id in dict.fromkeys(candidate_ids):
        try:
            lesson = registry.select(scope, candidate_id, canary_key)
        except LessonPromotionNotFound:
            continue
        if lesson is not None:
            selected.append(lesson)
    return tuple(selected)


def route_context_domains(
    *,
    user_memory: Callable[[], ContextResult] | None,
    agent_learning: Callable[[], tuple[LessonUse, ...]] | None,
    organizational_knowledge: Callable[[], KnowledgeSearchResult] | None,
) -> CrossDomainRetrieval:
    warnings: list[str] = []
    statuses: list[DomainRouteStatus] = []

    def retrieve(domain: str, operation: Callable[[], object] | None) -> object | None:
        if operation is None:
            warnings.append(f"domain_unauthorized:{domain}")
            statuses.append(DomainRouteStatus(domain, False, False, 0))
            return None
        try:
            result = operation()
        except SQLAlchemyError:
            warnings.append(f"domain_unavailable:{domain}")
            statuses.append(DomainRouteStatus(domain, True, False, 0))
            return None
        if isinstance(result, ContextResult):
            count = sum(len(section.items) for section in result.sections)
            warnings.extend(f"{domain}:{warning}" for warning in result.warnings)
        elif isinstance(result, KnowledgeSearchResult):
            count = len(result.passages)
            warnings.extend(f"{domain}:{warning}" for warning in result.warnings)
        else:
            count = len(result)  # type: ignore[arg-type]
        statuses.append(DomainRouteStatus(domain, True, True, count))
        return result

    user_result = retrieve("user_memory", user_memory)
    lesson_result = retrieve("agent_learning", agent_learning)
    knowledge_result = retrieve("organizational_knowledge", organizational_knowledge)
    nested_partial = bool(
        (isinstance(user_result, ContextResult) and user_result.partial)
        or (
            isinstance(knowledge_result, KnowledgeSearchResult)
            and knowledge_result.partial
        )
    )
    return CrossDomainRetrieval(
        user_memory=user_result if isinstance(user_result, ContextResult) else None,
        agent_learning=lesson_result if isinstance(lesson_result, tuple) else None,
        organizational_knowledge=(
            knowledge_result
            if isinstance(knowledge_result, KnowledgeSearchResult)
            else None
        ),
        domains=tuple(statuses),
        warnings=tuple(dict.fromkeys(warnings)),
        partial=nested_partial or any(not status.available for status in statuses),
    )


__all__ = [
    "ContextItem",
    "ContextResult",
    "ContextSection",
    "CrossDomainRetrieval",
    "DomainRouteStatus",
    "UserContextService",
    "route_context_domains",
    "select_agent_lessons",
]
