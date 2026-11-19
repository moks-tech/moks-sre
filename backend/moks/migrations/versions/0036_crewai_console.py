"""CrewAI console (docs/crewai-console.md): run history, schedules and versioned crew configuration.

- crew_runs: one row per CrewAI investigation, numbered per flow (#1, #2, …) through crew_run_counters, with its
  trigger (user, rerun, schedule, test), the inputs it ran with and the crew configuration version it used.
- crew_run_steps, crew_run_logs, crew_run_calls: what the console shows of a run. Steps are the crew's stages; logs
  are the lines the CrewAI worker writes while it runs; calls are model-call transcripts, kept only while the
  "transcripts" setting is on. Retention deletes old log lines and transcripts; runs and steps stay.
- crew_schedules: cron schedules in the database, fired by the worker's scheduler as the person who saved them.
- crew_configs: versions of a crew's editable surface (prompts, tools from the allowlist, limits). At most one is
  active per crew; none active means the code's defaults.
- crew_settings: small settings (run history, flows switched off).

Revision ID: 0036
Revises: 0035
"""
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

TABLES = ("crew_run_calls", "crew_run_logs", "crew_run_steps", "crew_runs", "crew_run_counters", "crew_schedules", "crew_configs", "crew_settings")


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("""CREATE TABLE crew_run_counters (
        flow varchar(60) PRIMARY KEY,
        last integer NOT NULL DEFAULT 0)""")
    bind.exec_driver_sql("""CREATE TABLE crew_schedules (
        id uuid PRIMARY KEY,
        flow varchar(60) NOT NULL,
        name varchar(120) NOT NULL,
        cron varchar(120) NOT NULL,
        inputs jsonb NOT NULL DEFAULT '{}',
        enabled boolean NOT NULL DEFAULT true,
        owner_id uuid NOT NULL REFERENCES users(id),
        version integer NOT NULL DEFAULT 1,
        next_run_at timestamptz,
        last_run_at timestamptz,
        last_run_id uuid,
        last_error text,
        created_by uuid REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_by uuid REFERENCES users(id),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (flow, name))""")
    bind.exec_driver_sql("CREATE INDEX crew_schedules_due ON crew_schedules (next_run_at) WHERE enabled")
    bind.exec_driver_sql("""CREATE TABLE crew_configs (
        id uuid PRIMARY KEY,
        crew varchar(60) NOT NULL,
        version integer NOT NULL,
        config jsonb NOT NULL,
        digest varchar(64) NOT NULL,
        base_digest varchar(64) NOT NULL,
        note text NOT NULL DEFAULT '',
        active boolean NOT NULL DEFAULT false,
        created_by uuid REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        activated_at timestamptz,
        UNIQUE (crew, version))""")
    bind.exec_driver_sql("CREATE UNIQUE INDEX crew_configs_one_active ON crew_configs (crew) WHERE active")
    bind.exec_driver_sql("""CREATE TABLE crew_settings (
        key varchar(60) PRIMARY KEY,
        value jsonb NOT NULL,
        updated_by uuid REFERENCES users(id),
        updated_at timestamptz NOT NULL DEFAULT now())""")
    bind.exec_driver_sql("""CREATE TABLE crew_runs (
        investigation_id uuid PRIMARY KEY REFERENCES investigations(id) ON DELETE CASCADE,
        flow varchar(60) NOT NULL,
        run_number integer NOT NULL,
        trigger varchar(20) NOT NULL CHECK (trigger IN ('user', 'rerun', 'schedule', 'test')),
        inputs jsonb NOT NULL DEFAULT '{}',
        schedule_id uuid REFERENCES crew_schedules(id) ON DELETE SET NULL,
        rerun_of uuid,
        config_version integer,
        config_digest varchar(64) NOT NULL DEFAULT '',
        created_by uuid REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (flow, run_number))""")
    bind.exec_driver_sql("CREATE INDEX crew_runs_recent ON crew_runs (created_at DESC)")
    bind.exec_driver_sql("""CREATE TABLE crew_run_steps (
        id bigserial PRIMARY KEY,
        investigation_id uuid NOT NULL REFERENCES crew_runs(investigation_id) ON DELETE CASCADE,
        step varchar(30) NOT NULL,
        position integer NOT NULL,
        label varchar(120) NOT NULL,
        status varchar(12) NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled', 'skipped')),
        started_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz,
        error text,
        model_calls integer NOT NULL DEFAULT 0,
        tool_calls integer NOT NULL DEFAULT 0,
        tokens_in integer NOT NULL DEFAULT 0,
        tokens_out integer NOT NULL DEFAULT 0,
        cost_usd numeric(12, 6) NOT NULL DEFAULT 0,
        UNIQUE (investigation_id, step))""")
    bind.exec_driver_sql("""CREATE TABLE crew_run_logs (
        id bigserial PRIMARY KEY,
        investigation_id uuid NOT NULL REFERENCES crew_runs(investigation_id) ON DELETE CASCADE,
        step varchar(30) NOT NULL,
        at timestamptz NOT NULL DEFAULT now(),
        level varchar(8) NOT NULL DEFAULT 'info',
        message text NOT NULL,
        data jsonb,
        call_id bigint)""")
    bind.exec_driver_sql("CREATE INDEX crew_run_logs_run ON crew_run_logs (investigation_id, id)")
    bind.exec_driver_sql("CREATE INDEX crew_run_logs_age ON crew_run_logs (at)")
    bind.exec_driver_sql("""CREATE TABLE crew_run_calls (
        id bigserial PRIMARY KEY,
        investigation_id uuid NOT NULL REFERENCES crew_runs(investigation_id) ON DELETE CASCADE,
        step varchar(30) NOT NULL,
        provider varchar(20) NOT NULL DEFAULT '',
        model varchar(200) NOT NULL DEFAULT '',
        system text NOT NULL DEFAULT '',
        prompt jsonb NOT NULL DEFAULT '[]',
        response text NOT NULL DEFAULT '',
        tokens_in integer NOT NULL DEFAULT 0,
        tokens_out integer NOT NULL DEFAULT 0,
        cost_usd numeric(12, 6) NOT NULL DEFAULT 0,
        duration_ms integer NOT NULL DEFAULT 0,
        outcome varchar(20) NOT NULL DEFAULT 'ok',
        error text,
        created_at timestamptz NOT NULL DEFAULT now())""")
    bind.exec_driver_sql("CREATE INDEX crew_run_calls_run ON crew_run_calls (investigation_id, id)")
    bind.exec_driver_sql("CREATE INDEX crew_run_calls_age ON crew_run_calls (created_at)")


def downgrade() -> None:
    bind = op.get_bind()
    for t in TABLES:
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
