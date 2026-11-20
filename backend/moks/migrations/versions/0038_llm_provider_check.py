"""LLM providers: the database accepts every provider the API offers.

0001 allowed openai, gemini, bedrock and ollama. anthropic and azure_openai were added to the API later without
widening the check, so saving one failed, and the failure was reported as a duplicate name.

Revision ID: 0038
Revises: 0037
"""
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

PROVIDERS = ("anthropic", "openai", "azure_openai", "gemini", "bedrock", "ollama")


def _check(providers: tuple[str, ...]) -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE llm_configs DROP CONSTRAINT IF EXISTS llm_configs_provider_check")
    names = ", ".join(f"'{p}'" for p in providers)
    bind.exec_driver_sql(f"ALTER TABLE llm_configs ADD CONSTRAINT llm_configs_provider_check CHECK (provider IN ({names}))")


def upgrade() -> None:
    _check(PROVIDERS)


def downgrade() -> None:
    # Fails while an anthropic or azure_openai row exists: delete those first.
    _check(("openai", "gemini", "bedrock", "ollama"))
