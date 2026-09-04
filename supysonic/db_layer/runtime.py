import importlib
import os.path
from contextlib import contextmanager
from threading import local
from urllib.parse import urlparse

from playhouse.db_url import parseresult_to_dict, schemes

from .core import Meta, db
from .schema import SCHEMA_VERSION, execute_sql_resource_script, list_migrations


_connection_scope_state = local()


def init_database(database_uri):
    uri = urlparse(database_uri)
    args = parseresult_to_dict(uri)
    if uri.scheme.startswith("mysql"):
        args.setdefault("charset", "utf8mb4")
        args.setdefault("binary_prefix", True)

    if uri.scheme.startswith("mysql"):
        provider = "mysql"
    elif uri.scheme.startswith("postgres"):
        provider = "postgres"
    elif uri.scheme.startswith("sqlite"):
        provider = "sqlite"
        args["pragmas"] = {"foreign_keys": 1}
    else:
        raise RuntimeError(f"Unsupported database: {uri.scheme}")

    db_class = schemes.get(uri.scheme)
    temp = db_class(**args)
    if uri.scheme == "sqlite":
        database_dir = os.path.dirname(temp.database)
        if database_dir:
            os.makedirs(database_dir, exist_ok=True)
    db.initialize(db_class(**args))
    db.connect()

    # Check if we should create the tables
    if not db.table_exists("meta"):
        with db.atomic():
            execute_sql_resource_script(f"schema/{provider}.sql")
            Meta.create(key="schema_version", value=SCHEMA_VERSION)

    # Check for schema changes
    version = Meta["schema_version"]
    if version.value < SCHEMA_VERSION:
        args.pop("pragmas", ())
        migrations = sorted(list_migrations(provider))
        for migration in migrations:
            if migration[0] in ("_", "."):
                continue

            date, ext = os.path.splitext(migration)
            if date <= version.value:
                continue

            if ext == ".sql":
                with db.atomic():
                    execute_sql_resource_script(
                        f"schema/migration/{provider}/{migration}"
                    )
            elif ext == ".py":
                m = importlib.import_module(
                    f".schema.migration.{provider}.{date}", "supysonic"
                )
                m.apply(args.copy())

        version.value = SCHEMA_VERSION
        version.save()


def release_database():
    db.close()
    db.initialize(None)


def open_connection(reuse=False):
    return db.connect(reuse)


def close_connection():
    if getattr(_connection_scope_state, "depth", 0) > 0:
        return
    db.close()


@contextmanager
def connection_scope(reuse=False):
    depth = getattr(_connection_scope_state, "depth", 0)
    owns_connection = depth == 0 and db.is_closed()
    if owns_connection:
        db.connect(reuse)
    _connection_scope_state.depth = depth + 1
    try:
        yield db
    finally:
        _connection_scope_state.depth = depth
        if depth == 0:
            del _connection_scope_state.depth
        if owns_connection and not db.is_closed():
            db.close()
