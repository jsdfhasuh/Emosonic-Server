#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
suffix=$$
network="supysonic-emo-migrations-$suffix"
postgres="supysonic-emo-postgres-$suffix"
mariadb="supysonic-emo-mariadb-$suffix"
postgres_runner="supysonic-emo-postgres-runner-$suffix"
mariadb_runner="supysonic-emo-mariadb-runner-$suffix"

cleanup() {
    docker rm -f "$postgres_runner" "$mariadb_runner" \
        "$postgres" "$mariadb" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker network create "$network" >/dev/null
docker run --detach --name "$postgres" --network "$network" \
    --tmpfs /var/lib/postgresql/data \
    --env POSTGRES_DB=supysonic_clean \
    --env POSTGRES_PASSWORD=supysonic \
    --env POSTGRES_USER=supysonic \
    postgres:17-alpine >/dev/null
docker run --detach --name "$mariadb" --network "$network" \
    --tmpfs /var/lib/mysql \
    --env MARIADB_DATABASE=supysonic_clean \
    --env MARIADB_PASSWORD=supysonic \
    --env MARIADB_ROOT_PASSWORD=supysonic-root \
    --env MARIADB_USER=supysonic \
    mariadb:11.4 >/dev/null

until docker exec "$postgres" pg_isready -U supysonic -d supysonic_clean >/dev/null 2>&1; do
    sleep 1
done
until docker exec "$mariadb" healthcheck.sh --connect --innodb_initialized >/dev/null 2>&1; do
    sleep 1
done

docker create --name "$postgres_runner" --network "$network" \
    --env PGDATABASE=supysonic_clean \
    --env PGHOST="$postgres" \
    --env PGPASSWORD=supysonic \
    --env PGUSER=supysonic \
    postgres:17-alpine \
    /bin/sh /workspace/tests/emo_migrations/postgres.sh >/dev/null
docker cp "$repo_root/." "$postgres_runner:/workspace"
docker start --attach "$postgres_runner"

docker create --name "$mariadb_runner" --network "$network" \
    --env MYSQL_HOST="$mariadb" \
    --env MYSQL_PWD=supysonic-root \
    --env MYSQL_USER=root \
    mariadb:11.4 \
    /bin/sh /workspace/tests/emo_migrations/mariadb.sh >/dev/null
docker cp "$repo_root/." "$mariadb_runner:/workspace"
docker start --attach "$mariadb_runner"
