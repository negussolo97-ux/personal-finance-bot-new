import os
from contextlib import contextmanager
from sqlalchemy import create_engine, Integer, BigInteger, String, Text, JSON, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

class Base(DeclarativeBase):
    pass

class Lock(Base):
    __tablename__ = 'app_lock'
    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[dict] = mapped_column(JSON, default=dict)

class Event(Base):
    __tablename__ = 'sms_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    raw: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(default='review')

class Tx(Base):
    __tablename__ = 'transactions'
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    account: Mapped[str]
    direction: Mapped[str]
    amount: Mapped[int] = mapped_column(BigInteger)
    fee: Mapped[int] = mapped_column(BigInteger, default=0)
    occurred_at: Mapped[str]
    category: Mapped[str] = mapped_column(default='pending')
    person: Mapped[str] = mapped_column(default='')
    debt_id: Mapped[int | None] = mapped_column(nullable=True)
    principal: Mapped[int] = mapped_column(BigInteger, default=0)
    interest: Mapped[int] = mapped_column(BigInteger, default=0)
    pair_id: Mapped[int | None] = mapped_column(nullable=True)
    note: Mapped[str] = mapped_column(Text, default='')

class Debt(Base):
    __tablename__ = 'debts'
    id: Mapped[int] = mapped_column(primary_key=True)
    side: Mapped[str]  # owe / owed
    person: Mapped[str]
    opening: Mapped[int] = mapped_column(BigInteger, default=0)
    due: Mapped[str] = mapped_column(default='')

class Update(Base):
    __tablename__ = 'telegram_updates'
    id: Mapped[int] = mapped_column(primary_key=True)

class Outbox(Base):
    __tablename__ = 'outbox'
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(unique=True)
    method: Mapped[str] = mapped_column(default='sendMessage')
    payload: Mapped[dict] = mapped_column(JSON)
    sent: Mapped[bool] = mapped_column(default=False)
    attempts: Mapped[int] = mapped_column(BigInteger, default=0)
    next_try: Mapped[float] = mapped_column(default=0.0)

class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[str]
    action: Mapped[str]
    detail: Mapped[dict] = mapped_column(JSON)

class Store:
    def __init__(self, url):
        if url.startswith(('postgres://', 'postgresql://')):
            url = 'postgresql+psycopg://' + url.split('://', 1)[1]
        kwargs = dict(pool_pre_ping=True)
        if url == 'sqlite://':
            kwargs.update(poolclass=StaticPool, connect_args={'check_same_thread': False})
        self.engine = create_engine(url, **kwargs)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        with self.Session.begin() as s:
            if s.get(Lock, 1) is None:
                s.add(Lock(id=1, state={}))
        # Single Gunicorn worker; threads serialize through this lock. PostgreSQL row lock also
        # serializes changes across old/new processes during a deployment.
        import threading
        self.mutex = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.mutex, self.Session.begin() as s:
            s.execute(select(Lock).where(Lock.id == 1).with_for_update()).scalar_one()
            yield s
