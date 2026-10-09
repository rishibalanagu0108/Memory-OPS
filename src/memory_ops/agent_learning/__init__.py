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


__all__ = [
    "AgentCheckpoint",
    "AgentLearningStore",
    "AgentRunScope",
    "ObservableFact",
    "StructuredEpisode",
    "ToolIdentity",
]
