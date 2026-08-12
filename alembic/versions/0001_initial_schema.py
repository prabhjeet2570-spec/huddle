"""Initial schema: tenancy, reservations, conversations and reliability telemetry.

Revision ID: 25619cc03af8
Revises: 
Create Date: 2026-09-05 19:36:35.979445
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('alerts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('conversation_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=48), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_alerts_kind_time', 'alerts', ['kind', 'created_at'], unique=False)
    op.create_table('calendar_entries',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('reservation_id', sa.UUID(), nullable=False),
    sa.Column('external_id', sa.String(length=64), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('reservation_id')
    )
    op.create_table('conversations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('outcome', sa.String(length=16), nullable=True),
    sa.Column('escalation_reason', sa.Text(), nullable=True),
    sa.Column('tool_calls_used', sa.Integer(), nullable=False),
    sa.Column('tokens_used', sa.Integer(), nullable=False),
    sa.Column('turns', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_conversations_user', 'conversations', ['user_id', 'created_at'], unique=False)
    op.create_table('hold_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('room_id', sa.UUID(), nullable=False),
    sa.Column('reservation_id', sa.UUID(), nullable=False),
    sa.Column('event', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_hold_events_user_time', 'hold_events', ['user_id', 'created_at'], unique=False)
    op.create_table('hold_rate_limits',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('until', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_hold_rate_limits_user', 'hold_rate_limits', ['user_id', 'until'], unique=False)
    op.create_table('notifications',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('reservation_id', sa.UUID(), nullable=False),
    sa.Column('channel', sa.String(length=32), nullable=False),
    sa.Column('recipient', sa.String(length=128), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_notifications_reservation', 'notifications', ['reservation_id'], unique=False)
    op.create_table('organizations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('sagas',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=True),
    sa.Column('reservation_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('failed_step', sa.String(length=64), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('tool_invocations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('turn_id', sa.UUID(), nullable=True),
    sa.Column('tool_name', sa.String(length=64), nullable=False),
    sa.Column('risk', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('error_type', sa.String(length=64), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('latency_ms', sa.Float(), nullable=False),
    sa.Column('arguments', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_tool_invocations_conv', 'tool_invocations', ['conversation_id'], unique=False)
    op.create_index('ix_tool_invocations_name_status', 'tool_invocations', ['tool_name', 'status'], unique=False)
    op.create_table('conversation_messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('tool_calls', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('tool_call_id', sa.String(length=64), nullable=True),
    sa.Column('name', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conversation_id', 'seq', name='uq_messages_seq')
    )
    op.create_index('ix_messages_conversation', 'conversation_messages', ['conversation_id', 'seq'], unique=False)
    op.create_table('pending_confirmations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('tool_name', sa.String(length=64), nullable=False),
    sa.Column('arguments', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_pending_confirmations_conv', 'pending_confirmations', ['conversation_id', 'status'], unique=False)
    op.create_table('rooms',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.Column('capacity', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('capacity > 0', name='ck_rooms_capacity_positive'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('org_id', 'name', name='uq_rooms_org_name')
    )
    op.create_table('saga_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('saga_id', sa.UUID(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('step', sa.String(length=64), nullable=False),
    sa.Column('phase', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['saga_id'], ['sagas.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_saga_events_saga', 'saga_events', ['saga_id', 'seq'], unique=False)
    op.create_table('turns',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('latency_ms', sa.Float(), nullable=False),
    sa.Column('llm_calls', sa.Integer(), nullable=False),
    sa.Column('tool_calls', sa.Integer(), nullable=False),
    sa.Column('prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('completion_tokens', sa.Integer(), nullable=False),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('trace_id', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conversation_id', 'seq', name='uq_turns_seq')
    )
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('org_id', 'username', name='uq_users_org_name')
    )
    op.create_table('reservations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('reference', sa.String(length=16), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('room_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('attendees', sa.Integer(), nullable=False),
    sa.Column('period', postgresql.TSTZRANGE(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('hold_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(state <> 'held') OR (hold_expires_at IS NOT NULL)", name='ck_reservations_hold_has_deadline'),
    sa.CheckConstraint("state IN ('held', 'confirmed', 'cancelled', 'expired')", name='ck_reservations_state'),
    sa.CheckConstraint('attendees > 0', name='ck_reservations_attendees_positive'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['room_id'], ['rooms.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('reference')
    )
    op.create_index('ix_reservations_hold_expiry', 'reservations', ['hold_expires_at'], unique=False, postgresql_where="state = 'held'")
    op.create_index('ix_reservations_org_state', 'reservations', ['org_id', 'state'], unique=False)
    op.create_index('ix_reservations_user_state', 'reservations', ['user_id', 'state'], unique=False)


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('ix_reservations_user_state', table_name='reservations')
    op.drop_index('ix_reservations_org_state', table_name='reservations')
    op.drop_index('ix_reservations_hold_expiry', table_name='reservations', postgresql_where="state = 'held'")
    op.drop_table('reservations')
    op.drop_table('users')
    op.drop_table('turns')
    op.drop_index('ix_saga_events_saga', table_name='saga_events')
    op.drop_table('saga_events')
    op.drop_table('rooms')
    op.drop_index('ix_pending_confirmations_conv', table_name='pending_confirmations')
    op.drop_table('pending_confirmations')
    op.drop_index('ix_messages_conversation', table_name='conversation_messages')
    op.drop_table('conversation_messages')
    op.drop_index('ix_tool_invocations_name_status', table_name='tool_invocations')
    op.drop_index('ix_tool_invocations_conv', table_name='tool_invocations')
    op.drop_table('tool_invocations')
    op.drop_table('sagas')
    op.drop_table('organizations')
    op.drop_index('ix_notifications_reservation', table_name='notifications')
    op.drop_table('notifications')
    op.drop_index('ix_hold_rate_limits_user', table_name='hold_rate_limits')
    op.drop_table('hold_rate_limits')
    op.drop_index('ix_hold_events_user_time', table_name='hold_events')
    op.drop_table('hold_events')
    op.drop_index('ix_conversations_user', table_name='conversations')
    op.drop_table('conversations')
    op.drop_table('calendar_entries')
    op.drop_index('ix_alerts_kind_time', table_name='alerts')
    op.drop_table('alerts')
