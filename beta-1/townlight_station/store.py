# SPDX-License-Identifier: AGPL-3.0-only
from contextlib import contextmanager
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData(schema='station')
version = sa.Table('schema_version', metadata, sa.Column('version', sa.Integer, primary_key=True))
recordings = sa.Table('recordings', metadata,
    sa.Column('id', sa.Uuid(as_uuid=False), primary_key=True),
    sa.Column('creator', sa.String(64), nullable=False),
    sa.Column('idempotency_key', sa.String(100), nullable=False),
    sa.Column('request_hash', sa.String(64), nullable=False),
    sa.Column('meeting_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('status', sa.String(30), nullable=False),
    sa.Column('details', JSONB, nullable=False),
    sa.Column('control', JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint('creator', 'idempotency_key'))

# A schedule is durable intent: the worker dispatches it into a normal recording
# job once its start time arrives, so restart never loses or double-runs it.
schedules = sa.Table('schedules', metadata,
    sa.Column('id', sa.Uuid(as_uuid=False), primary_key=True),
    sa.Column('creator', sa.String(64), nullable=False),
    sa.Column('idempotency_key', sa.String(100), nullable=False),
    sa.Column('request_hash', sa.String(64), nullable=False),
    sa.Column('meeting_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(30), nullable=False),
    sa.Column('details', JSONB, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint('creator', 'idempotency_key'))

# A playout is commit-to-air of a completed recording. It is NOT publication.
playouts = sa.Table('playouts', metadata,
    sa.Column('id', sa.Uuid(as_uuid=False), primary_key=True),
    sa.Column('creator', sa.String(64), nullable=False),
    sa.Column('idempotency_key', sa.String(100), nullable=False),
    sa.Column('request_hash', sa.String(64), nullable=False),
    sa.Column('recording_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('status', sa.String(30), nullable=False),
    sa.Column('details', JSONB, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint('creator', 'idempotency_key'))

# Single-row on-air state so switching/return-to-schedule survives restarts.
station_state = sa.Table('station_state', metadata,
    sa.Column('id', sa.Integer, primary_key=True),
    sa.Column('on_air', JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")))


class Store:
    def __init__(self, url):
        self.engine = sa.create_engine(url, pool_pre_ping=True)

    def migrate(self):
        with self.engine.begin() as conn:
            conn.execute(sa.text('CREATE SCHEMA IF NOT EXISTS station'))
            metadata.create_all(conn)
            if conn.scalar(sa.select(version.c.version)) is None:
                conn.execute(version.insert().values(version=1))
            conn.execute(sa.text("ALTER TABLE station.recordings ADD COLUMN IF NOT EXISTS control jsonb NOT NULL DEFAULT '{}'::jsonb"))
            conn.execute(sa.text("INSERT INTO station.station_state (id, on_air) VALUES (1, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"))
            conn.execute(version.update().where(version.c.version.in_((1, 2))).values(version=3))
            self.check(conn)

    def check(self, conn):
        if conn.execute(sa.select(version.c.version)).scalars().all() != [3]:
            raise RuntimeError('Station schema migration required')

    def ready(self):
        with self.engine.connect() as conn:
            self.check(conn)

    def get(self, identity):
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(recordings).where(recordings.c.id == identity)).mappings().first()
            return dict(row) if row else None

    def update(self, identity, status, details):
        with self.engine.begin() as conn:
            conn.execute(recordings.update().where(recordings.c.id == identity).values(status=status, details=details))

    def on_air(self):
        with self.engine.connect() as conn:
            return conn.scalar(sa.select(station_state.c.on_air).where(station_state.c.id == 1)) or {}

    @contextmanager
    def worker_lock(self):
        with self.engine.connect() as conn:
            acquired = conn.scalar(sa.text('SELECT pg_try_advisory_lock(1953721716)'))
            conn.commit()
            try:
                yield bool(acquired)
            finally:
                if acquired:
                    conn.execute(sa.text('SELECT pg_advisory_unlock(1953721716)'))
                    conn.commit()
