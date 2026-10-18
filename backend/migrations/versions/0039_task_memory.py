"""Application-scoped task-memory sources and rebuildable summaries."""
from alembic import op
import sqlalchemy as sa
revision = '0039_task_memory'
down_revision = '0038_memory_application_scope_validity'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('task_memory_sources',
        sa.Column('source_id', sa.String(64), primary_key=True),
        sa.Column('owner_id', sa.String(128), nullable=False),
        sa.Column('application_id', sa.String(128), nullable=False),
        sa.Column('task_id', sa.String(40), sa.ForeignKey('career_tasks.task_id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_kind', sa.String(32), nullable=False),
        sa.Column('source_key', sa.String(128), nullable=False),
        sa.Column('task_version', sa.Integer(), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False))
    for name in ('owner_id', 'application_id', 'task_id'):
        op.create_index('ix_task_memory_sources_'+name, 'task_memory_sources', [name])
    op.create_table('task_memory_summaries',
        sa.Column('summary_id', sa.String(64), primary_key=True),
        sa.Column('owner_id', sa.String(128), nullable=False),
        sa.Column('application_id', sa.String(128), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('generator_version', sa.String(40), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.UniqueConstraint('owner_id', 'application_id', name='uq_task_memory_scope'))

def downgrade():
    op.drop_table('task_memory_summaries')
    op.drop_table('task_memory_sources')
