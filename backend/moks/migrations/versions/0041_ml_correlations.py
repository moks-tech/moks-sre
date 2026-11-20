"""Correlation groups for anomaly detection (moks/ml/correlate.py, docs/ML.md): anomalies that probably belong to
one problem, with the links that tie them and unvalidated candidate explanations. Shadow output only. Operators can
judge a group (learning_judgments target_kind correlation_group), which is review input like any other judgment.

Revision ID: 0041
Revises: 0040
"""
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("""CREATE TABLE ml_correlations (
        id uuid PRIMARY KEY,
        environment varchar(100) NOT NULL,
        namespaces jsonb NOT NULL DEFAULT '[]',
        status varchar(20) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
        first_seen timestamptz NOT NULL,
        last_seen timestamptz NOT NULL,
        members jsonb NOT NULL DEFAULT '[]',
        links jsonb NOT NULL DEFAULT '[]',
        hypotheses jsonb NOT NULL DEFAULT '[]',
        context jsonb NOT NULL DEFAULT '{}',
        engine_version varchar(40) NOT NULL,
        mode varchar(20) NOT NULL,
        feedback jsonb NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""")
    bind.exec_driver_sql("CREATE INDEX ml_correlations_recent ON ml_correlations (environment, status, last_seen DESC)")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ml_observations_model ON ml_observations (model_id, observed_at DESC)")  # model health reads by model
    bind.exec_driver_sql("ALTER TABLE learning_judgments DROP CONSTRAINT learning_judgments_target_kind_check")
    bind.exec_driver_sql("""ALTER TABLE learning_judgments ADD CONSTRAINT learning_judgments_target_kind_check CHECK (target_kind IN
        ('passage', 'similar_incident', 'skill_recommendation', 'anomaly_observation', 'correlation_group'))""")


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DELETE FROM learning_judgments WHERE target_kind = 'correlation_group'")
    bind.exec_driver_sql("ALTER TABLE learning_judgments DROP CONSTRAINT learning_judgments_target_kind_check")
    bind.exec_driver_sql("""ALTER TABLE learning_judgments ADD CONSTRAINT learning_judgments_target_kind_check CHECK (target_kind IN
        ('passage', 'similar_incident', 'skill_recommendation', 'anomaly_observation'))""")
    bind.exec_driver_sql("DROP INDEX IF EXISTS ml_observations_model")
    bind.exec_driver_sql("DROP TABLE IF EXISTS ml_correlations")
