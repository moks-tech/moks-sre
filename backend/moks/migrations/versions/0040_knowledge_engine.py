"""SRE knowledge engine (docs/KNOWLEDGE.md): document types and per-chunk metadata, when a document's content last
changed, an embedding cache so unchanged text is never embedded twice, and investigation history as a source type.

Revision ID: 0040
Revises: 0039
"""
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run = op.get_bind().exec_driver_sql
    run("ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_type varchar(30)")
    run("ALTER TABLE documents ADD COLUMN IF NOT EXISTS resource_types jsonb NOT NULL DEFAULT '[]'::jsonb")
    # updated_at also moves when a document is only read again (a pipeline change), so it cannot answer "what changed".
    run("ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_changed_at timestamptz")
    run("UPDATE documents SET content_changed_at = updated_at WHERE content_changed_at IS NULL")
    run("CREATE INDEX IF NOT EXISTS ix_documents_doc_type ON documents (doc_type)")
    run("CREATE INDEX IF NOT EXISTS ix_documents_content_changed_at ON documents (content_changed_at)")
    # Postmortems keep no original text, so they are never read again: their type is known from their source.
    run("UPDATE documents d SET doc_type = 'postmortem' FROM knowledge_sources s WHERE s.id = d.source_id AND s.type = 'incident' AND d.doc_type IS NULL")
    run("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS meta jsonb")
    run("ALTER TABLE knowledge_sources DROP CONSTRAINT IF EXISTS knowledge_sources_type_check")
    run("ALTER TABLE knowledge_sources ADD CONSTRAINT knowledge_sources_type_check CHECK (type IN ('github','upload','incident','web','investigation'))")
    # A vector per model and exact input text. No text is kept: only its SHA-256. A document that changes is
    # embedded only where its text changed, and a rebuild with the same model reads its vectors from here.
    run("""CREATE TABLE IF NOT EXISTS embedding_cache (
        model_key varchar(300) NOT NULL,
        input_hash varchar(64) NOT NULL,
        dimensions integer NOT NULL CHECK (dimensions > 0),
        vector bytea NOT NULL CHECK (length(vector) = dimensions * 4),
        created_at timestamptz NOT NULL DEFAULT now(),
        used_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (model_key, input_hash))""")
    run("CREATE INDEX IF NOT EXISTS ix_embedding_cache_used_at ON embedding_cache (used_at)")


def downgrade() -> None:
    run = op.get_bind().exec_driver_sql
    run("DROP TABLE IF EXISTS embedding_cache")
    run("DELETE FROM knowledge_sources WHERE type = 'investigation'")
    run("ALTER TABLE knowledge_sources DROP CONSTRAINT IF EXISTS knowledge_sources_type_check")
    run("ALTER TABLE knowledge_sources ADD CONSTRAINT knowledge_sources_type_check CHECK (type IN ('github','upload','incident','web'))")
    run("ALTER TABLE chunks DROP COLUMN IF EXISTS meta")
    run("DROP INDEX IF EXISTS ix_documents_content_changed_at")
    run("DROP INDEX IF EXISTS ix_documents_doc_type")
    run("ALTER TABLE documents DROP COLUMN IF EXISTS content_changed_at")
    run("ALTER TABLE documents DROP COLUMN IF EXISTS resource_types")
    run("ALTER TABLE documents DROP COLUMN IF EXISTS doc_type")
