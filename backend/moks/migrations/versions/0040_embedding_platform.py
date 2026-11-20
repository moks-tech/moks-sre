"""Embedding platform: provider-independent embedding options, generation specs and reusable vectors.

- llm_configs.embedding_options: query and document prefixes, truncation and model revision (moks/rag/embeddings.py).
  Empty for every existing row, which keeps each model key, and so each index, exactly as it was.
- Two embedding providers: huggingface (a Text Embeddings Inference API, including MOKS's embedding service) and
  sentence_transformers (in-process). provider widens to 30 characters for the second, here and in llm_usage.
- embedding_indexes.spec records what produced a generation's vectors; reused_vectors how many a rebuild copied
  instead of embedding again.
- chunk_embeddings.input_hash identifies a vector by model key and exact model input, so a rebuild with the same
  model reuses vectors instead of calling the model for text it has already embedded. Existing rows have none and
  are embedded again once, by the next rebuild.

Revision ID: 0040
Revises: 0039
"""
from alembic import op

revision = "0040"
down_revision = "0039"
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
    run("ALTER TABLE chunk_embeddings ADD COLUMN IF NOT EXISTS input_hash varchar(64)")
    run("CREATE INDEX IF NOT EXISTS chunk_embeddings_input_hash ON chunk_embeddings (input_hash) WHERE input_hash IS NOT NULL")


def downgrade() -> None:
    # Fails while a huggingface or sentence_transformers row exists: delete those first.
    run = op.get_bind().exec_driver_sql
    run("DROP INDEX IF EXISTS chunk_embeddings_input_hash")
    run("ALTER TABLE chunk_embeddings DROP COLUMN IF EXISTS input_hash")
    run("ALTER TABLE embedding_indexes DROP COLUMN IF EXISTS reused_vectors")
    run("ALTER TABLE embedding_indexes DROP COLUMN IF EXISTS spec")
    run("ALTER TABLE llm_configs DROP COLUMN IF EXISTS embedding_options")
    _check(OLD)
    run("ALTER TABLE llm_usage ALTER COLUMN provider TYPE varchar(20)")
    run("ALTER TABLE llm_configs ALTER COLUMN provider TYPE varchar(20)")
