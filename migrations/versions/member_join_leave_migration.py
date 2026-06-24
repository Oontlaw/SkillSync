from alembic import op
import sqlalchemy as sa
from datetime import datetime

revision = 'member_join_leave_migration'
down_revision = '6271b49286ae'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('member_join_leave',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('guild_id', sa.String(50), nullable=False, index=True),
        sa.Column('member_id', sa.String(50), nullable=False, index=True),
        sa.Column('member_name', sa.String(100), nullable=False),
        sa.Column('is_bot', sa.Boolean(), default=False),
        sa.Column('event_type', sa.String(10), nullable=False, index=True),
        sa.Column('leave_reason', sa.String(50), nullable=True),
        sa.Column('hour_of_day', sa.Integer(), nullable=True),
        sa.Column('day_of_week', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), default=datetime.utcnow, index=True),
    )

def downgrade():
    op.drop_table('member_join_leave')