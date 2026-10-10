import ast
from pathlib import Path


ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations/versions"
RESULT = Path(__file__).with_name("migration-result.json")


def migration_metadata(path: Path) -> tuple[str, str | None, ast.Module]:
    tree = ast.parse(path.read_text(), filename=str(path))
    values: dict[str, str | None] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                values[target.id] = ast.literal_eval(node.value)
    return values["revision"], values["down_revision"], tree


def test_schema_migrations_are_linear_reversible_and_expand_only() -> None:
    previous = None
    for path in sorted(VERSIONS.glob("*.py")):
        revision, down_revision, tree = migration_metadata(path)
        assert down_revision == previous
        assert revision == path.stem
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert {"upgrade", "downgrade"} <= functions.keys()
        upgrade = ast.get_source_segment(path.read_text(), functions["upgrade"]) or ""
        assert "drop_table(" not in upgrade
        assert "drop_column(" not in upgrade
        previous = revision


def test_vocabulary_backfill_precedes_constraints_and_has_rollback() -> None:
    source = (VERSIONS / "0005_canonical_memory_vocabulary.py").read_text()
    upgrade, downgrade = source.split("def downgrade", maxsplit=1)
    assert upgrade.index("DISABLE TRIGGER") < upgrade.index("UPDATE user_memory_versions")
    assert upgrade.index("UPDATE user_memory_versions") < upgrade.index("create_check_constraint")
    assert upgrade.index("create_check_constraint") < upgrade.index("ENABLE TRIGGER")
    assert "UPDATE user_memory_versions" in downgrade
    assert "sensitivity IN ('public', 'internal', 'confidential', 'restricted')" in downgrade


def test_derived_artifacts_keep_canonical_index_and_model_identity() -> None:
    vector = (VERSIONS / "0009_vector_retrieval.py").read_text()
    chunks = (VERSIONS / "0013_knowledge_document_chunks.py").read_text()
    for field in ("canonical_version_id", "index_generation", "model_name", "model_version"):
        assert field in vector
    for field in (
        "document_version_id",
        "index_generation",
        "projection_model",
        "projection_model_version",
    ):
        assert field in chunks
    assert "ON DELETE CASCADE" in vector
    assert "ondelete=\"CASCADE\"" in chunks
    downgrade = chunks.split("def downgrade", maxsplit=1)[1]
    assert "ck_knowledge_document_versions_media_type" not in downgrade


def test_disposable_branch_drill_proves_backfill_and_rollback() -> None:
    result = __import__("json").loads(RESULT.read_text())
    assert result["data_loss_count"] == 0
    assert result["rollback_proven"] is True
    assert result["upgraded_values"] == ["normal", "explicit"]
    assert result["rolled_back_values"] == ["internal", "user"]
