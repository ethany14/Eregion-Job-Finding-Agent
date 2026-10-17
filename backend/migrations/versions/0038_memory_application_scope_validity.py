"""Memory stage 2: APPLICATION scope type and fact validity time (valid_from / valid_to).

Additive: two nullable columns and a wider scope check. Existing rows keep NULL validity
(unknown, never guessed) and their scope values.
"""
from alembic import op
import sqlalchemy as sa

revision = '0038_memory_application_scope_validity'
down_revision = '0037_external_tool_operations'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('memory_items', recreate='always') as batch:
        batch.add_column(sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True))
        batch.drop_constraint('ck_memory_items_scope', type_='check')
        batch.create_check_constraint('ck_memory_items_scope',
                                      "scope_type IN ('user','project','session','application')")
    op.create_index('ix_memory_items_valid_from', 'memory_items', ['valid_from'])
    op.create_index('ix_memory_items_valid_to', 'memory_items', ['valid_to'])


def downgrade():
    op.drop_index('ix_memory_items_valid_to', 'memory_items')
    op.drop_index('ix_memory_items_valid_from', 'memory_items')
    with op.batch_alter_table('memory_items', recreate='always') as batch:
        batch.drop_constraint('ck_memory_items_scope', type_='check')
        batch.create_check_constraint('ck_memory_items_scope', "scope_type IN ('user','project','session')")
        batch.drop_column('valid_to')
        batch.drop_column('valid_from')
