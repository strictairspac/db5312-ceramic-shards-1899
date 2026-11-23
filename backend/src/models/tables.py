from sqlalchemy import (JSON, Column, ForeignKey, Integer, MetaData, String,
                        Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))

# Transactional outbox: business writes and the records to be handed over commit
# or roll back together.  Each row is immutable once written; the identity is a
# monotonically increasing commit position used for ordered replay.
outbox = Table('handover_outbox', schema,
    Column('id', Integer, primary_key=True),
    Column('seq', Integer, nullable=False, unique=True, index=True),
    Column('kind', String(20), nullable=False, index=True),
    Column('reference', String(50), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('created_at', String(32), nullable=False),
    UniqueConstraint('kind', 'reference', name='uq_handover_outbox_kind_reference'))

# The handover ledger (交班账).  One row per business event, recorded once for
# all crews; receipts are retried and rows may be delivered more than once, so
# the unique outbox identity makes replay idempotent.  ``crew`` records which
# crew confirmed the entry first (informational).
ledger = Table('handover_ledger', schema,
    Column('id', Integer, primary_key=True),
    Column('outbox_id', ForeignKey('handover_outbox.id'), nullable=False, unique=True),
    Column('crew', String(40), nullable=False),
    Column('seq', Integer, nullable=False, unique=True, index=True),
    Column('kind', String(20), nullable=False, index=True),
    Column('reference', String(50), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('recorded_at', String(32), nullable=False))

# One checkpoint per crew.  A crew only advances its own cursor, so one crew's
# stalled handover never blocks another crew's progress; the ledger itself is
# shared and written once.
cursors = Table('handover_cursors', schema,
    Column('crew', String(40), primary_key=True),
    Column('last_seq', Integer, nullable=False),
    Column('updated_at', String(32), nullable=False))

# Single counter row used to assign commit-ordered outbox positions.  The UPDATE
# holds the row lock until the business transaction commits, so a lower seq is
# always committed before a higher one: replay can safely resume from a cursor
# even when an older transaction commits late.
sequence = Table('handover_sequence', schema,
    Column('name', String(40), primary_key=True),
    Column('value', Integer, nullable=False))
