"""Add RoleChangeLog model

Revision ID: 19eedc79b37
Revises: a1b2c3d4e5f6
Create Date: 2026-06-22 10:52:22.000000

"""
revision = '19eedc79b37'
down_revision = 'c84ae5824645'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.create_table('role_change_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.String(length=50), nullable=False),
        sa.Column('member_id', sa.String(length=50), nullable=False),
        sa.Column('member_name', sa.String(length=100), nullable=False),
        sa.Column('change_type', sa.String(length=20), nullable=False),
        sa.Column('role_id', sa.String(length=50), nullable=False),
        sa.Column('role_name', sa.String(length=100), nullable=False),
        sa.Column('change_category', sa.String(length=30), nullable=False),
        sa.Column('was_staff_before', sa.Boolean(), nullable=True),
        sa.Column('is_staff_now', sa.Boolean(), nullable=True),
        sa.Column('modifier_id', sa.String(length=50), nullable=True),
        sa.Column('modifier_name', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_role_change_log_guild_id'), 'role_change_log', ['guild_id'], unique=False)
    op.create_index(op.f('ix_role_change_log_member_id'), 'role_change_log', ['member_id'], unique=False)
    op.create_index(op.f('ix_role_change_log_created_at'), 'role_change_log', ['created_at'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_role_change_log_member_id'), table_name='role_change_log')
    op.drop_index(op.f('ix_role_change_log_guild_id'), table_name='role_change_log')
    op.drop_index(op.f('ix_role_change_log_created_at'), table_name='role_change_log')
    op.drop_table('role_change_log')
