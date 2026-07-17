import sys

from .core import db

SCHEMA_VERSION = "20260717"
RESOURCE_PACKAGE = "supysonic"


if sys.version_info < (3, 9):
    import pkg_resources

    def get_resource_text(respath):
        return pkg_resources.resource_string(RESOURCE_PACKAGE, respath).decode("utf-8")

    def list_migrations(provider):
        return pkg_resources.resource_listdir(
            RESOURCE_PACKAGE, f"schema/migration/{provider}"
        )

else:
    import importlib.resources

    def get_resource_text(respath):
        return (
            importlib.resources.files(RESOURCE_PACKAGE).joinpath(respath).read_text("utf-8")
        )

    def list_migrations(provider):
        return (
            e.name
            for e in importlib.resources.files(RESOURCE_PACKAGE)
            .joinpath(f"schema/migration/{provider}")
            .iterdir()
        )


def execute_sql_resource_script(respath):
    sql = get_resource_text(respath)
    for statement in sql.split(";"):
        statement = statement.strip()
        if statement and not statement.startswith("--"):
            db.execute_sql(statement)
