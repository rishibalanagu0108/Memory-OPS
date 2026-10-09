"""Scoped agent checkpoints and observable structured episodes."""

import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text

from memory_ops.persistence import TenantDatabase
from memory_ops.security import enforce_content_admission


BoundedText = Annotated[str, Field(min_length=1, max_length=255)]
ObservableValue = str | int | float | bool | None
CheckpointState = Literal["active", "waiting", "completed", "failed"]
EpisodeType = Literal["action", "tool_call", "outcome", "error"]
OutcomeStatus = Literal["success", "failure", "partial"]
LessonVersionStage = Literal["evaluated", "promoted"]
_PROHIBITED_OBSERVATION_NAMES = {
    "chain_of_thought",
    "credentials",
    "private_reasoning",
    "raw_tool_input",
    "raw_tool_output",
    "reasoning",
    "scratchpad",
    "secret",
    "tool_input",
    "tool_output",
}


class AgentLearningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentRunScope(AgentLearningModel):
    tenant_id: UUID
    workspace_id: UUID
    agent_id: UUID
    run_id: UUID
    task_type: BoundedText
    environment: BoundedText


class ObservableFact(AgentLearningModel):
    name: BoundedText
    value: ObservableValue

    @field_validator("name")
    @classmethod
    def reject_private_or_raw_fields(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", " ").replace(" ", "_")
        if normalized in _PROHIBITED_OBSERVATION_NAMES:
            raise ValueError("private reasoning, credentials, and raw tool content are prohibited")
        return value

    @model_validator(mode="after")
    def admit_string_value(self) -> "ObservableFact":
        if isinstance(self.value, str):
            if not self.value.strip():
                raise ValueError("observable string values must contain content")
            if len(self.value) > 2_000:
                raise ValueError("observable string values cannot exceed 2000 characters")
            enforce_content_admission(self.value)
        return self


class ToolIdentity(AgentLearningModel):
    name: BoundedText
    version: BoundedText


class LessonScope(AgentLearningModel):
    tenant_id: UUID
    workspace_id: UUID
    agent_id: UUID
    task_type: BoundedText
    environment: BoundedText
    tool: ToolIdentity


class LessonModelIdentity(AgentLearningModel):
    provider: BoundedText
    name: BoundedText
    version: BoundedText


class LessonEvidenceReference(AgentLearningModel):
    episode_id: UUID
    run_id: UUID


class CandidateLesson(AgentLearningModel):
    id: UUID = Field(default_factory=uuid4)
    scope: LessonScope
    title: BoundedText
    procedure: Annotated[str, Field(min_length=1, max_length=4_000)]
    evidence: Annotated[tuple[LessonEvidenceReference, ...], Field(min_length=1)]
    generator: LessonModelIdentity

    @model_validator(mode="after")
    def admit_candidate_content(self) -> "CandidateLesson":
        if len({item.episode_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("candidate lesson evidence must be unique")
        for value in (self.title, self.procedure):
            if not value.strip():
                raise ValueError("candidate lesson content must contain text")
            enforce_content_admission(value)
        return self


class LessonVersion(AgentLearningModel):
    id: UUID = Field(default_factory=uuid4)
    candidate_id: UUID
    scope: LessonScope
    version_number: Annotated[int, Field(ge=1)]
    stage: LessonVersionStage = "evaluated"
    title: BoundedText
    procedure: Annotated[str, Field(min_length=1, max_length=4_000)]
    evaluation_reference: BoundedText
    evaluation_contract_version: BoundedText
    evaluation_dataset_version: BoundedText
    source_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    generator: LessonModelIdentity

    @model_validator(mode="after")
    def admit_version_content(self) -> "LessonVersion":
        for value in (self.title, self.procedure):
            if not value.strip():
                raise ValueError("lesson version content must contain text")
            enforce_content_admission(value)
        return self


class AgentCheckpoint(AgentLearningModel):
    id: UUID = Field(default_factory=uuid4)
    scope: AgentRunScope
    sequence: Annotated[int, Field(ge=1)]
    state: CheckpointState
    observations: tuple[ObservableFact, ...] = ()


class StructuredEpisode(AgentLearningModel):
    id: UUID = Field(default_factory=uuid4)
    scope: AgentRunScope
    checkpoint_id: UUID | None = None
    episode_type: EpisodeType
    action: BoundedText
    outcome: Annotated[str, Field(min_length=1, max_length=2_000)]
    outcome_status: OutcomeStatus
    tool: ToolIdentity | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def admit_observable_content(self) -> "StructuredEpisode":
        if not self.action.strip() or not self.outcome.strip():
            raise ValueError("episode action and outcome must contain content")
        for value in (self.action, self.outcome):
            enforce_content_admission(value)
        return self


class AgentLearningStore:
    def __init__(self, database: TenantDatabase) -> None:
        self.database = database

    def save_checkpoint(self, checkpoint: AgentCheckpoint) -> UUID:
        scope = checkpoint.scope
        observations = [fact.model_dump(mode="json") for fact in checkpoint.observations]
        with self.database.transaction(scope.tenant_id) as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO agent_checkpoints (
                        id, tenant_id, workspace_id, agent_id, run_id,
                        task_type, environment, sequence, state, observations
                    ) VALUES (
                        :id, :tenant_id, :workspace_id, :agent_id, :run_id,
                        :task_type, :environment, :sequence, :state,
                        CAST(:observations AS jsonb)
                    )
                    """
                ),
                {
                    "id": checkpoint.id,
                    "tenant_id": scope.tenant_id,
                    "workspace_id": scope.workspace_id,
                    "agent_id": scope.agent_id,
                    "run_id": scope.run_id,
                    "task_type": scope.task_type,
                    "environment": scope.environment,
                    "sequence": checkpoint.sequence,
                    "state": checkpoint.state,
                    "observations": json.dumps(observations),
                },
            )
        return checkpoint.id

    def save_episode(self, episode: StructuredEpisode) -> UUID:
        scope = episode.scope
        with self.database.transaction(scope.tenant_id) as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO agent_episodes (
                        id, tenant_id, workspace_id, agent_id, run_id,
                        checkpoint_id, task_type, environment, episode_type,
                        action, outcome, outcome_status, tool_name, tool_version,
                        observed_at
                    ) VALUES (
                        :id, :tenant_id, :workspace_id, :agent_id, :run_id,
                        :checkpoint_id, :task_type, :environment, :episode_type,
                        :action, :outcome, :outcome_status, :tool_name,
                        :tool_version, :observed_at
                    )
                    """
                ),
                {
                    "id": episode.id,
                    "tenant_id": scope.tenant_id,
                    "workspace_id": scope.workspace_id,
                    "agent_id": scope.agent_id,
                    "run_id": scope.run_id,
                    "checkpoint_id": episode.checkpoint_id,
                    "task_type": scope.task_type,
                    "environment": scope.environment,
                    "episode_type": episode.episode_type,
                    "action": episode.action,
                    "outcome": episode.outcome,
                    "outcome_status": episode.outcome_status,
                    "tool_name": episode.tool.name if episode.tool else None,
                    "tool_version": episode.tool.version if episode.tool else None,
                    "observed_at": episode.observed_at,
                },
            )
        return episode.id

    def save_candidate_lesson(self, candidate: CandidateLesson) -> UUID:
        scope = candidate.scope
        tool = scope.tool
        generator = candidate.generator
        with self.database.transaction(scope.tenant_id) as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO agent_lesson_candidates (
                        id, tenant_id, workspace_id, agent_id, task_type,
                        environment, tool_name, tool_version, title, procedure,
                        generator_provider, generator_name, generator_version
                    ) VALUES (
                        :id, :tenant_id, :workspace_id, :agent_id, :task_type,
                        :environment, :tool_name, :tool_version, :title,
                        :procedure, :generator_provider, :generator_name,
                        :generator_version
                    )
                    """
                ),
                {
                    "id": candidate.id,
                    "tenant_id": scope.tenant_id,
                    "workspace_id": scope.workspace_id,
                    "agent_id": scope.agent_id,
                    "task_type": scope.task_type,
                    "environment": scope.environment,
                    "tool_name": tool.name,
                    "tool_version": tool.version,
                    "title": candidate.title,
                    "procedure": candidate.procedure,
                    "generator_provider": generator.provider,
                    "generator_name": generator.name,
                    "generator_version": generator.version,
                },
            )
            for evidence in candidate.evidence:
                connection.execute(
                    text(
                        """
                        INSERT INTO agent_lesson_candidate_evidence (
                            tenant_id, workspace_id, agent_id, task_type,
                            environment, tool_name, tool_version, candidate_id,
                            evidence_run_id, episode_id
                        ) VALUES (
                            :tenant_id, :workspace_id, :agent_id, :task_type,
                            :environment, :tool_name, :tool_version,
                            :candidate_id, :evidence_run_id, :episode_id
                        )
                        """
                    ),
                    {
                        "tenant_id": scope.tenant_id,
                        "workspace_id": scope.workspace_id,
                        "agent_id": scope.agent_id,
                        "task_type": scope.task_type,
                        "environment": scope.environment,
                        "tool_name": tool.name,
                        "tool_version": tool.version,
                        "candidate_id": candidate.id,
                        "evidence_run_id": evidence.run_id,
                        "episode_id": evidence.episode_id,
                    },
                )
        return candidate.id

    def save_evaluated_lesson(self, version: LessonVersion) -> UUID:
        if version.stage != "evaluated":
            raise ValueError("promotion requires the M5 promotion service")
        return self._save_lesson_version(version)

    def _save_lesson_version(self, version: LessonVersion) -> UUID:
        scope = version.scope
        tool = scope.tool
        generator = version.generator
        with self.database.transaction(scope.tenant_id) as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO agent_lesson_versions (
                        id, tenant_id, workspace_id, agent_id, task_type,
                        environment, tool_name, tool_version, candidate_id,
                        version_number, stage, title, procedure,
                        evaluation_reference, evaluation_contract_version,
                        evaluation_dataset_version, source_hash,
                        generator_provider, generator_name, generator_version
                    ) VALUES (
                        :id, :tenant_id, :workspace_id, :agent_id, :task_type,
                        :environment, :tool_name, :tool_version, :candidate_id,
                        :version_number, :stage, :title, :procedure,
                        :evaluation_reference, :evaluation_contract_version,
                        :evaluation_dataset_version, :source_hash,
                        :generator_provider, :generator_name, :generator_version
                    )
                    """
                ),
                {
                    "id": version.id,
                    "tenant_id": scope.tenant_id,
                    "workspace_id": scope.workspace_id,
                    "agent_id": scope.agent_id,
                    "task_type": scope.task_type,
                    "environment": scope.environment,
                    "tool_name": tool.name,
                    "tool_version": tool.version,
                    "candidate_id": version.candidate_id,
                    "version_number": version.version_number,
                    "stage": version.stage,
                    "title": version.title,
                    "procedure": version.procedure,
                    "evaluation_reference": version.evaluation_reference,
                    "evaluation_contract_version": version.evaluation_contract_version,
                    "evaluation_dataset_version": version.evaluation_dataset_version,
                    "source_hash": version.source_hash,
                    "generator_provider": generator.provider,
                    "generator_name": generator.name,
                    "generator_version": generator.version,
                },
            )
        return version.id


__all__ = [
    "AgentCheckpoint",
    "AgentLearningStore",
    "AgentRunScope",
    "CandidateLesson",
    "LessonEvidenceReference",
    "LessonModelIdentity",
    "LessonScope",
    "LessonVersion",
    "ObservableFact",
    "StructuredEpisode",
    "ToolIdentity",
]


from memory_ops.agent_learning.promotion import (  # noqa: E402
    LessonControlDecision,
    LessonControlState,
    LessonMonitoringMetrics,
    LessonPromotionEvidence,
    LessonPromotionRegistry,
    LessonUse,
)

__all__ += [
    "LessonControlDecision",
    "LessonControlState",
    "LessonMonitoringMetrics",
    "LessonPromotionEvidence",
    "LessonPromotionRegistry",
    "LessonUse",
]
