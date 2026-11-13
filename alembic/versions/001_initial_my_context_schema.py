"""Initial My Context schema (user_profiles, documents, memories)

Revision ID: 001_initial
Revises: 
Create Date: 2026-11-10 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. user_profiles
    op.create_table(
        'user_profiles',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.String(length=128), nullable=False),
        sa.Column('profile_data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_user_profiles_user_id'), 'user_profiles', ['user_id'], unique=True)

    # 2. documents
    op.create_table(
        'documents',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.String(length=128), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=128), nullable=False),
        sa.Column('file_size', sa.Integer(), nullable=False),
        sa.Column('storage_path', sa.String(length=512), nullable=True),
        sa.Column('file_data', sa.LargeBinary(), nullable=True),
        sa.Column('extracted_text', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_documents_user_id'), 'documents', ['user_id'], unique=False)
    op.create_index('ix_documents_user_created', 'documents', ['user_id', 'created_at'], unique=False)

    # 3. memories
    op.create_table(
        'memories',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.String(length=128), nullable=False),
        sa.Column('memory_content', sa.Text(), nullable=False),
        sa.Column('category', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_memories_user_id'), 'memories', ['user_id'], unique=False)
    op.create_index(op.f('ix_memories_category'), 'memories', ['category'], unique=False)
    op.create_index('ix_memories_user_category', 'memories', ['user_id', 'category'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_memories_user_category', table_name='memories')
    op.drop_index(op.f('ix_memories_category'), table_name='memories')
    op.drop_index(op.f('ix_memories_user_id'), table_name='memories')
    op.drop_table('memories')

    op.drop_index('ix_documents_user_created', table_name='documents')
    op.drop_index(op.f('ix_documents_user_id'), table_name='documents')
    op.drop_table('documents')

    op.drop_index(op.f('ix_user_profiles_user_id'), table_name='user_profiles')
    op.drop_table('user_profiles')

