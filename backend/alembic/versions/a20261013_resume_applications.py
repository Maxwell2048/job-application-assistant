"""Persist application status for each generated resume version."""
from alembic import op
import sqlalchemy as sa

revision = "a20261013"
down_revision = "e5a3928d4274"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("resume_applications"):
        return  # local SQLite startup may already have created this new table
    op.create_table("resume_applications",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("resume_applications")
