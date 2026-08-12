"""Stop double booking in the database rather than in Python.

The constraint mixes an equality operator on a scalar column with an overlap
operator on a range column, which stock GiST cannot index, so it needs the
btree_gist extension. The partial WHERE clause is what lets cancelled and
expired rows stay in the table as an audit trail without blocking anybody.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        """
        ALTER TABLE reservations
        ADD CONSTRAINT ex_reservations_no_overlap
        EXCLUDE USING gist (room_id WITH =, period WITH &&)
        WHERE (state IN ('held', 'confirmed'))
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE reservations DROP CONSTRAINT IF EXISTS ex_reservations_no_overlap"
    )
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
