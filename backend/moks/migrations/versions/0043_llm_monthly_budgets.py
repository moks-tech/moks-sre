"""LLM budgets: a monthly budget per LLM, and daily and monthly totals per provider.

- llm_configs.monthly_budget_usd: what one LLM may cost in a UTC calendar month. NULL (every existing row) means no
  monthly limit, so nothing changes until an administrator sets one in Settings → LLM providers.
- llm_provider_budgets: one optional row per provider (anthropic, openai, …) capping all of that provider's LLMs
  together, for example the credit on one Anthropic account. A NULL column is no limit; no row is no limit at all.
- llm_usage_provider_time: the provider totals sum usage by provider and time, not by LLM.

Revision ID: 0043
Revises: 0042
"""
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run = op.get_bind().exec_driver_sql
    run("ALTER TABLE llm_configs ADD COLUMN IF NOT EXISTS monthly_budget_usd numeric(12,4)")
    run("""
        CREATE TABLE IF NOT EXISTS llm_provider_budgets (
            provider varchar(30) PRIMARY KEY,
            daily_budget_usd numeric(12,4),
            monthly_budget_usd numeric(12,4),
            updated_by uuid,
            updated_at timestamptz NOT NULL DEFAULT now()
        )""")
    run("CREATE INDEX IF NOT EXISTS llm_usage_provider_time ON llm_usage (provider, created_at)")


def downgrade() -> None:
    run = op.get_bind().exec_driver_sql
    run("DROP INDEX IF EXISTS llm_usage_provider_time")
    run("DROP TABLE IF EXISTS llm_provider_budgets")
    run("ALTER TABLE llm_configs DROP COLUMN IF EXISTS monthly_budget_usd")
