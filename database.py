import peewee
from peewee import *
from datetime import datetime,timedelta
import os
from typing import Optional,List,Dict,Set,Iterable,Tuple,Any
import os as _os

def _resolve_db_path() -> str:
    """Pick a writable location for the SQLite file.

    Three locations are tried, in order:
      1. ``$LIQUIDITYHELPER_DB_PATH`` — operator escape hatch (tests
         and one-off migrations set this).
      2. ``$BITCART_DATADIR/plugin_data/liquidityhelper/`` — when
         loaded as a plugin, bitcart provides this guaranteed-writable
         dir, owned by the electrum user and survives container
         recreate.
      3. Directory next to this file — standalone fallback for
         developer machines and the regtest rig. The CWD-relative
         original (``liquidityhelper.sqlite``) only worked when the
         engine ran from the plugin root; bitcart launches us with
         CWD=/app where the electrum user can't write, which surfaced
         as a ``peewee.OperationalError: unable to open database file``
         on every dashboard/logs call.
    """
    override = _os.environ.get("LIQUIDITYHELPER_DB_PATH")
    if override:
        return override
    # bitcart's Settings class derives DATADIR from BITCART_DATADIR or
    # falls back to the constant "/datadir" mount path. The env var
    # isn't exported into the worker process by default, so probe both
    # — the env var first (lets tests point us elsewhere), then the
    # well-known mount.
    for candidate in (_os.environ.get("BITCART_DATADIR"), "/datadir"):
        if candidate and _os.path.isdir(candidate):
            plugin_data = _os.path.join(candidate, "plugin_data", "liquidityhelper")
            try:
                _os.makedirs(plugin_data, exist_ok=True)
            except PermissionError:
                continue
            if _os.access(plugin_data, _os.W_OK):
                return _os.path.join(plugin_data, "liquidityhelper.sqlite")
    # Last resort: alongside this file. This makes sense for the
    # regtest rig (where the rig owner controls CWD) and for unit
    # tests, but is mostly here as a non-crashing fallback rather
    # than an intentional production target.
    return _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "liquidityhelper.sqlite")

DATABASE_NAME = _resolve_db_path()
db = SqliteDatabase(DATABASE_NAME)

class BaseModel(Model):
    """Base model that all models will inherit from"""

    class Meta:
        database = db

class LOrder(BaseModel):
    """Model for liquidity orders table"""
    order_id = CharField(max_length=255)
    date = DateTimeField()

    class Meta:
        table_name = 'lrequests'

class SimpleDateTimeField(BaseModel):
    """Most-recent-timestamp-per-key store. Used to record when various
    cashout / fee-payment attempts happened so downstream code can ask
    "how long since LN last succeeded?"

    `name` is unique: `Model.replace(name=..., date=...)` upserts on it,
    keeping exactly one row per key.
    """
    name = CharField(max_length=255, unique=True)
    date = DateTimeField()
class Notification(BaseModel):
    """Model for notifications table"""
    type =CharField(max_length=15) # Valid options: LOWLIQ
    body = TextField(null=True)
    date_sent = DateTimeField(null=True)
class LastRunTracker(BaseModel):
    name = CharField(unique=True)
    last_run = DateTimeField(default=datetime.now())
class SimpleCacheField(BaseModel):
    """Model for cache table"""
    name = CharField(max_length=100)
    date = DateTimeField()
    content=TextField()
    expiry_in_seconds = IntegerField()

    @classmethod
    def delete_expired(cls):
        """Delete all expired cache entries.

        Returns:
            int: Number of deleted entries
        """
        now = datetime.now()
        # Delete all records where current time > date + expiry_in_seconds
        count = cls.delete().where(
            cls.date + cls.expiry_in_seconds < now.timestamp()
        ).execute()
        return count
class SimpleVariable(BaseModel):
    name = CharField(max_length=100,primary_key=True)
    value = CharField()


def create_order(order_id, date=None):
    """
    Create a new liquidity request entry

    Args:
        order_id (str): Order ID
        date (datetime, optional): Date of order. Defaults to current time.

    Returns:
        LRequest: Created request object
    """
    if date is None:
        date = datetime.now()

    request = LOrder.create(order_id=order_id, date=date)
    print(f"Created order with ID: {order_id}")
    return request
def count_notifications_sent(since_date:Optional[datetime]=None, notification_type:Optional[str]=None)->int:
    """
    Count how many notifications have been sent since datetime x of type y.
    """
    found_records=[]
    records=Notification.select()
    for record in records:
        found_records.append(str(record.__dict__))
    pass
    if notification_type:
        if since_date:
            found_notifications: int = Notification.select().where(Notification.date_sent >= since_date,
                                                                                  Notification.type == notification_type).count()
        else:
            found_notifications: int = Notification.select().where(Notification.date_sent.is_null(False),
                                                                                  Notification.type == notification_type).count()
    else:
        if since_date:
            found_notifications: int = Notification.select().where(Notification.date_sent >= since_date,
                                                                              Notification.date_sent.is_null(False)).count()
        else:
            found_notifications: int = Notification.select().where(Notification.date_sent !=None,
                                                                              Notification.date_sent.is_null(False)).count()

    return found_notifications

db.connect(reuse_if_open=True)
USED_TABLES=[SimpleDateTimeField,SimpleCacheField,LOrder,LastRunTracker,SimpleVariable,Notification]
db.create_tables(USED_TABLES, safe=True)


def _migrate_simpledatetimefield_uniqueness(_db=db) -> None:
    """Idempotent migration for pre-existing DBs that were written under
    the old (non-unique) schema. Two things to fix:

      1. Old code did `SimpleDateTimeField.replace(name=..., date=...).execute()`
         without a unique constraint on `name`, so each call inserted a
         new row. Tables accumulated duplicates. Dedupe by keeping the
         highest-id row per name (= most recently inserted).
      2. Add the unique index on `name`. `create_tables(safe=True)` won't
         add a UNIQUE constraint to an existing table — only `CREATE
         INDEX IF NOT EXISTS` works for that on SQLite.

    Safe to run on every startup. No-op when the DB is already clean.
    """
    try:
        _db.execute_sql(
            "DELETE FROM simpledatetimefield WHERE id NOT IN ("
            " SELECT MAX(id) FROM simpledatetimefield GROUP BY name"
            ")"
        )
        _db.execute_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uniq_simpledatetimefield_name ON simpledatetimefield (name)"
        )
    except Exception as e:
        # Failure here shouldn't crash startup — the feature degrades
        # gracefully (duplicates linger but get_last_date still returns
        # the most-recent row via order_by(date.desc())).
        print(f"SimpleDateTimeField migration skipped: {e}")


_migrate_simpledatetimefield_uniqueness()
print(f"Database '{DATABASE_NAME}' initialized successfully")