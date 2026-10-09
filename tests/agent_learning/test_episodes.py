from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from memory_ops.agent_learning import (
    AgentCheckpoint,
    AgentLearningStore,
    AgentRunScope,
    ObservableFact,
    StructuredEpisode,
    ToolIdentity,
)
from memory_ops.config import Settings
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.security import ProhibitedContent


@pytest.fixture(scope="module")
def learning_context() -> tuple[Engine, TenantDatabase, AgentRunScope, UUID]:
    settings = Settings.from_environment()
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    tenant_id, other_tenant_id = uuid4(), uuid4()
    workspace_id, other_workspace_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:tenant), (:other)"),
            {"tenant": tenant_id, "other": other_tenant_id},
        )
        connection.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id) "
                "VALUES (:workspace, :tenant), (:other_workspace, :other)"
            ),
            {
                "workspace": workspace_id,
                "tenant": tenant_id,
                "other_workspace": other_workspace_id,
                "other": other_tenant_id,
            },
        )
    database = TenantDatabase(engine)
    scope = AgentRunScope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        agent_id=uuid4(),
        run_id=uuid4(),
        task_type="python-maintenance",
        environment="test",
    )
    yield engine, database, scope, other_tenant_id
    engine.dispose()


def test_store_scoped_checkpoint_and_observable_episode(
    learning_context: tuple[Engine, TenantDatabase, AgentRunScope, UUID],
) -> None:
    _, database, scope, _ = learning_context
    store = AgentLearningStore(database)
    checkpoint = AgentCheckpoint(
        scope=scope,
        sequence=1,
        state="active",
        observations=(
            ObservableFact(name="tests_failed", value=1),
            ObservableFact(name="failure_type", value="assertion mismatch"),
        ),
    )
    episode = StructuredEpisode(
        scope=scope,
        checkpoint_id=checkpoint.id,
        episode_type="tool_call",
        action="Run the focused regression test",
        outcome="The regression test passed",
        outcome_status="success",
        tool=ToolIdentity(name="pytest", version="9.0"),
    )

    assert store.save_checkpoint(checkpoint) == checkpoint.id
    assert store.save_episode(episode) == episode.id

    with database.transaction(scope.tenant_id) as connection:
        stored_checkpoint = connection.execute(
            text(
                "SELECT workspace_id, agent_id, run_id, sequence, state, observations "
                "FROM agent_checkpoints WHERE id = :id"
            ),
            {"id": checkpoint.id},
        ).mappings().one()
        stored_episode = connection.execute(
            text(
                "SELECT checkpoint_id, episode_type, action, outcome, outcome_status, "
                "tool_name, tool_version FROM agent_episodes WHERE id = :id"
            ),
            {"id": episode.id},
        ).mappings().one()

    assert tuple(stored_checkpoint[key] for key in ("workspace_id", "agent_id", "run_id")) == (
        scope.workspace_id,
        scope.agent_id,
        scope.run_id,
    )
    assert stored_checkpoint["sequence"] == 1
    assert stored_checkpoint["state"] == "active"
    assert stored_checkpoint["observations"] == [
        {"name": "tests_failed", "value": 1},
        {"name": "failure_type", "value": "assertion mismatch"},
    ]
    assert tuple(stored_episode.values()) == (
        checkpoint.id,
        "tool_call",
        "Run the focused regression test",
        "The regression test passed",
        "success",
        "pytest",
        "9.0",
    )


def test_private_reasoning_secrets_and_raw_tool_content_are_rejected(
    learning_context: tuple[Engine, TenantDatabase, AgentRunScope, UUID],
) -> None:
    _, database, scope, _ = learning_context
    store = AgentLearningStore(database)

    with pytest.raises(ValidationError, match="private reasoning"):
        ObservableFact(name="chain_of_thought", value="hidden reasoning")
    with pytest.raises(ProhibitedContent):
        ObservableFact(name="status", value="api_key=do-not-store-this-value")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        StructuredEpisode.model_validate(
            {
                "scope": scope,
                "episode_type": "tool_call",
                "action": "Call a tool",
                "outcome": "Completed",
                "outcome_status": "success",
                "raw_tool_output": {"response": "arbitrary"},
            }
        )

    with database.transaction(scope.tenant_id) as connection:
        before = connection.execute(text("SELECT count(*) FROM agent_episodes")).scalar_one()
    with pytest.raises(ProhibitedContent):
        store.save_episode(
            StructuredEpisode(
                scope=scope,
                episode_type="outcome",
                action="Record authorization=Bearer-token-value",
                outcome="Completed",
                outcome_status="success",
            )
        )
    with database.transaction(scope.tenant_id) as connection:
        after = connection.execute(text("SELECT count(*) FROM agent_episodes")).scalar_one()
    assert after == before


def test_rls_scope_links_and_immutability_fail_closed(
    learning_context: tuple[Engine, TenantDatabase, AgentRunScope, UUID],
) -> None:
    engine, database, scope, other_tenant_id = learning_context
    checkpoint = AgentCheckpoint(scope=scope, sequence=2, state="waiting")
    AgentLearningStore(database).save_checkpoint(checkpoint)

    with database.transaction(other_tenant_id) as connection:
        assert connection.execute(text("SELECT count(*) FROM agent_checkpoints")).scalar_one() == 0

    mismatched_scope = scope.model_copy(update={"workspace_id": uuid4()})
    with pytest.raises(DBAPIError):
        AgentLearningStore(database).save_episode(
            StructuredEpisode(
                scope=mismatched_scope,
                checkpoint_id=checkpoint.id,
                episode_type="outcome",
                action="Finish task",
                outcome="Completed",
                outcome_status="success",
            )
        )

    with pytest.raises(DBAPIError, match="agent learning records are immutable"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE agent_checkpoints SET state = 'completed' WHERE id = :id"),
                {"id": checkpoint.id},
            )
