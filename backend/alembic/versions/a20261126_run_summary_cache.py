"""Run summary cache and an index for the board's sort order.

run_summary_cache holds derived data only (services/run_index.py); dropping it
loses nothing. ix_jobs_last_seen serves ORDER BY last_seen on the jobs list.
"""
from alembic import op
import sqlalchemy as sa

revision = "a20261126"
down_revision = "a20261013"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("run_summary_cache"):   # local start-up may already have created it
        op.create_table("run_summary_cache",
            sa.Column("run_id", sa.String(64), primary_key=True),
            sa.Column("signature", sa.String(64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("summary", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    if "ix_jobs_last_seen" not in {index["name"] for index in inspector.get_indexes("jobs")}:
        op.create_index("ix_jobs_last_seen", "jobs", ["last_seen"])


def downgrade():
    op.drop_index("ix_jobs_last_seen", table_name="jobs")
    op.drop_table("run_summary_cache")
