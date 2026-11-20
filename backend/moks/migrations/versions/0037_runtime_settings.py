"""Runtime settings (moks/runtime_settings.py, docs/SETTINGS.md): an administrator's value for one of the curated
deployment settings. No row means the Helm value applies; every change is also in the audit log.

Revision ID: 0037
Revises: 0036
"""
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("""CREATE TABLE runtime_settings (
        key varchar(80) PRIMARY KEY,
        value jsonb NOT NULL,
        reason text NOT NULL DEFAULT '',
        updated_by uuid REFERENCES users(id),
        updated_at timestamptz NOT NULL DEFAULT now())""")


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS runtime_settings")
