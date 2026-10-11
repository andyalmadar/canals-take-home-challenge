#!/bin/bash
# Runs once, on first boot of the compose Postgres volume (docker-entrypoint-initdb.d).
# Creates the database the test suite uses, alongside the main one.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE DATABASE orders_test OWNER "$POSTGRES_USER";
SQL
