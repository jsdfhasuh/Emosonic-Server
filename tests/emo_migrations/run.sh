#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
compose_file="$script_dir/docker-compose.yml"

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

compose -f "$compose_file" up --detach postgres mariadb
compose -f "$compose_file" run --rm postgres-test
compose -f "$compose_file" run --rm mariadb-test
