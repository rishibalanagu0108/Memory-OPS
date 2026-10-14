from pathlib import Path


ROOT = Path(__file__).parents[2]
OFFLINE = (ROOT / ".github/workflows/ci.yml").read_text()
DATABASE = (ROOT / ".github/workflows/database-ci.yml").read_text()


def test_offline_ci_requires_no_secrets_or_services() -> None:
    assert "pull_request_target" not in OFFLINE
    assert "${{ secrets." not in OFFLINE
    assert "permissions:\n  contents: read" in OFFLINE
    assert "uv sync --locked --dev" in OFFLINE
    assert "tests/performance/test_m8_latency_attribution.py" in OFFLINE
    assert (
        "tests/compatibility/test_api_and_sdks.py::"
        "test_typescript_sdk_builds_and_runs_compatibility_suite"
    ) in OFFLINE
    assert "npm test --prefix sdk/typescript" in OFFLINE


def test_database_ci_is_trusted_isolated_and_always_cleaned_up() -> None:
    required = (
        "github.event.pull_request.head.repo.full_name == github.repository",
        "permissions:\n  contents: read",
        "timeout-minutes: 30",
        "MEMORY_OPS_API_TOKEN: ci-test-token",
        "MEMORY_OPS_API_TENANT_ID: 00000000-0000-0000-0000-000000000001",
        "parent: testing",
        "branch_name: ci-run-${{ github.run_id }}-${{ github.run_attempt }}",
        "MEMORY_OPS_DATABASE_URL: ${{ steps.neon.outputs.db_url }}",
        "MEMORY_OPS_MIGRATION_DATABASE_URL: ${{ steps.neon.outputs.db_url }}",
        "run: uv run memory-ops-migrate",
        "run: uv run python -m pytest",
        "npm ci --prefix sdk/typescript",
        "if: ${{ always() && steps.neon.outputs.branch_id != '' }}",
        "branch: ${{ steps.neon.outputs.branch_id }}",
    )
    assert all(fragment in DATABASE for fragment in required)
    assert "pull_request_target" not in DATABASE
    assert "parent: production" not in DATABASE
    assert "suspend_timeout" not in DATABASE
    assert "neondatabase/create-branch-action@34f619c" in DATABASE
    assert "neondatabase/delete-branch-action@4468d825" in DATABASE
