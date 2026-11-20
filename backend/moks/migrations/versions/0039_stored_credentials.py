"""Stored credentials (moks/credentials.py, docs/CREDENTIALS.md): values an administrator pastes into Settings,
encrypted with AES-256-GCM. The database holds only ciphertext; the key comes from the environment.

Revision ID: 0039
Revises: 0038
"""
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("""CREATE TABLE stored_credentials (
        id uuid PRIMARY KEY,
        label varchar(200) NOT NULL,
        key_id varchar(16) NOT NULL,
        nonce bytea NOT NULL CHECK (length(nonce) = 12),
        ciphertext bytea NOT NULL,
        version integer NOT NULL DEFAULT 1,
        created_by uuid REFERENCES users(id),
        updated_by uuid REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""")


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS stored_credentials")
