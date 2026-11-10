#!/usr/bin/env bash
# Start PostgreSQL (with pgvector) and Redis on a GitHub-hosted Linux runner, and create the database $1.
# The runner's own packages are used instead of service containers: Docker Hub's unauthenticated pull
# limit made image pulls fail the job. The Postgres major version differs between runner images, so the
# pgvector package is chosen from the version that is installed.
set -euo pipefail

database="${1:?usage: ci_services.sh <database>}"

sudo apt-get update -qq
if [ ! -d /usr/lib/postgresql ]; then
  sudo apt-get install -y -qq postgresql
fi
major="$(ls /usr/lib/postgresql | sort -V | tail -1)"
sudo apt-get install -y -qq "postgresql-${major}-pgvector" redis-server
if [ -z "$(pg_lsclusters --no-header 2>/dev/null)" ]; then
  sudo pg_createcluster "$major" main
fi
sudo systemctl start postgresql.service redis-server.service
sudo -u postgres psql -qc "CREATE USER tuvl WITH SUPERUSER PASSWORD 'tuvl';"
sudo -u postgres createdb -O tuvl "$database"
echo "PostgreSQL ${major} and Redis are up; database ${database} created"
