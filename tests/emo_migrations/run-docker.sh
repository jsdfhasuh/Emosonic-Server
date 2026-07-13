#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
suffix=$$
network="supysonic-emo-migrations-$suffix"
postgres="supysonic-emo-postgres-$suffix"
mysql="supysonic-emo-mysql-$suffix"
runner="supysonic-emo-migration-runner-$suffix"

cleanup() {
    docker rm -f "$runner" "$postgres" "$mysql" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker network create "$network" >/dev/null
docker run --detach --name "$postgres" --network "$network" \
    --tmpfs /var/lib/postgresql/data \
    --env POSTGRES_DB=supysonic_test \
    --env POSTGRES_PASSWORD=supysonic \
    --env POSTGRES_USER=supysonic \
    postgres:16.9-alpine >/dev/null
docker run --detach --name "$mysql" --network "$network" \
    --tmpfs /var/lib/mysql \
    --env MYSQL_DATABASE=supysonic_test \
    --env MYSQL_PASSWORD=supysonic \
    --env MYSQL_ROOT_PASSWORD=supysonic-root \
    --env MYSQL_USER=supysonic \
    mysql:8.4.5 >/dev/null

until docker exec "$postgres" pg_isready -U supysonic -d supysonic_test >/dev/null 2>&1; do
    sleep 1
done
until docker exec "$mysql" mysqladmin ping --host=127.0.0.1 \
    --user=supysonic --password=supysonic --silent >/dev/null 2>&1; do
    sleep 1
done

docker create --name "$runner" --network "$network" \
    --env SUPYSONIC_TEST_POSTGRES_URI="postgresql://supysonic:supysonic@$postgres:5432/supysonic_test" \
    --env SUPYSONIC_TEST_MYSQL_URI="mysql://supysonic:supysonic@$mysql:3306/supysonic_test" \
    python:3.13-slim \
    /bin/sh -c \
    "python -m pip install --quiet -r /workspace/requirements.txt psycopg2-binary && \
     cd /workspace && \
     python -m unittest tests.base.test_emo_schema_migration" >/dev/null
docker cp "$repo_root/." "$runner:/workspace"
docker start --attach "$runner"
