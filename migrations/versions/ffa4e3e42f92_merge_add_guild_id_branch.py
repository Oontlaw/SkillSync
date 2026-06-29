"""merge add_guild_id branch

Revision ID: ffa4e3e42f92
Revises: c71ffb58358c, add_guild_id_community_burnout
Create Date: 2026-06-28 19:15:40.109792

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ffa4e3e42f92'
down_revision = ('c71ffb58358c', 'add_guild_id_community_burnout')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
