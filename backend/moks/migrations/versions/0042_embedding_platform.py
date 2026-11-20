"""Embedding platform: provider-independent embedding options, generation specs and reusable vectors.

- llm_configs.embedding_options: query and document prefixes, truncation and model revision (moks/rag/embeddings.py).
  Empty for every existing row, which keeps each model key, and so each index, exactly as it was.
- Two embedding providers: huggingface (a Text Embeddings Inference API, including MOKS's embedding service) and
  sentence_transformers (in-process). provider widens to 30 characters for the second, here and in llm_usage.
- embedding_indexes.spec records what produced a generation's vectors; reused_vectors how many a rebuild took from
  the embedding cache (0040) instead of embedding again. The cache's model key includes the embedding options'
  fingerprint, so a cached vector is reused only by a configuration that would produce the same one.

Revision ID: 0042
Revises: 0041
"""
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

PROVIDERS = ("anthropic", "openai", "azure_openai", "gemini", "bedrock", "ollama", "huggingface", "sentence_transformers")
OLD = ("anthropic", "openai", "azure_openai", "gemini", "bedrock", "ollama")


def _check(providers: tuple[str, ...]) -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE llm_configs DROP CONSTRAINT IF EXISTS llm_configs_provider_check")
    names = ", ".join(f"'{p}'" for p in providers)
    bind.exec_driver_sql(f"ALTER TABLE llm_configs ADD CONSTRAINT llm_configs_provider_check CHECK (provider IN ({names}))")


def upgrade() -> None:
    run = op.get_bind().exec_driver_sql
    run("ALTER TABLE llm_configs ALTER COLUMN provider TYPE varchar(30)")
    run("ALTER TABLE llm_usage ALTER COLUMN provider TYPE varchar(30)")
    _check(PROVIDERS)
    run("ALTER TABLE llm_configs ADD COLUMN IF NOT EXISTS embedding_options jsonb NOT NULL DEFAULT '{}'::jsonb")
    run("ALTER TABLE embedding_indexes ADD COLUMN IF NOT EXISTS spec jsonb NOT NULL DEFAULT '{}'::jsonb")
    run("ALTER TABLE embedding_indexes ADD COLUMN IF NOT EXISTS reused_vectors integer NOT NULL DEFAULT 0")


def downgrade() -> None:
    # Fails while a huggingface or sentence_transformers row exists: delete those first.
    run = op.get_bind().exec_driver_sql
    run("ALTER TABLE embedding_indexes DROP COLUMN IF EXISTS reused_vectors")
    run("ALTER TABLE embedding_indexes DROP COLUMN IF EXISTS spec")
    run("ALTER TABLE llm_configs DROP COLUMN IF EXISTS embedding_options")
    _check(OLD)
    run("ALTER TABLE llm_usage ALTER COLUMN provider TYPE varchar(20)")
    run("ALTER TABLE llm_configs ALTER COLUMN provider TYPE varchar(20)")
