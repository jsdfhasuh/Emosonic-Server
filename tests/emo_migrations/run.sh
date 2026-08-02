#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
compose_file="$repo_root/tests/compose.emo_migrations.yml"

if docker compose version >/dev/null 2>&1; then
    compose() {
        docker compose "$@"
    }
elif command -v docker-compose >/dev/null 2>&1; then
    compose() {
        docker-compose "$@"
    }
else
    exec sh "$script_dir/run-docker.sh"
fi

cleanup() {
    compose -f "$compose_file" down --volumes --remove-orphans
}
trap cleanup EXIT INT TERM

compose -f "$compose_file" up --detach --wait postgres mysql
export SUPYSONIC_TEST_POSTGRES_URI="${SUPYSONIC_TEST_POSTGRES_URI:-postgresql://supysonic:supysonic@127.0.0.1:55432/supysonic_test}"
export SUPYSONIC_TEST_MYSQL_URI="${SUPYSONIC_TEST_MYSQL_URI:-mysql://supysonic:supysonic@127.0.0.1:53306/supysonic_test}"
cd "$repo_root"
python -m unittest tests.base.test_emo_schema_migration
