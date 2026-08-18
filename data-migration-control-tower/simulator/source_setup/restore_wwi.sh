#!/usr/bin/env bash
# Downloads Microsoft's official WideWorldImporters OLTP backup and restores
# it into the local SQL Server container started by docker-compose.yml.
#
# Attribution: https://learn.microsoft.com/en-us/sql/samples/wide-world-importers-what-is
# License: MIT (Microsoft sql-server-samples repository)
#
# Usage: ./restore_wwi.sh   (run from simulator/source_setup/)
set -euo pipefail

# Git Bash on Windows auto-converts leading-/ arguments (e.g.
# /var/opt/mssql/backup) into Windows paths before they reach docker.exe,
# which breaks `docker exec`/`docker cp` calls targeting in-container
# paths. Disabling MSYS path conversion for this script avoids that.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONTAINER=legacy-sqlserver
SA_PASSWORD="${SQLSERVER_PASSWORD:-ChangeMe_Str0ng!}"
BAK_URL="https://github.com/Microsoft/sql-server-samples/releases/download/wide-world-importers-v1.0/WideWorldImporters-Full.bak"
BAK_FILE="WideWorldImporters-Full.bak"

echo "==> Waiting for SQL Server container to be healthy..."
for i in $(seq 1 30); do
  status="$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "starting")"
  if [ "$status" = "healthy" ]; then
    echo "    container healthy"
    break
  fi
  sleep 5
  if [ "$i" -eq 30 ]; then
    echo "ERROR: SQL Server container did not become healthy in time." >&2
    exit 1
  fi
done

if [ ! -f "$BAK_FILE" ]; then
  echo "==> Downloading WideWorldImporters-Full.bak (Microsoft official sample, MIT license)..."
  curl -L --fail -o "$BAK_FILE" "$BAK_URL"
else
  echo "==> Backup file already present locally, skipping download."
fi

echo "==> Copying backup into the container..."
docker cp "$BAK_FILE" "$CONTAINER":/var/opt/mssql/backup/"$BAK_FILE" 2>/dev/null || {
  docker exec "$CONTAINER" mkdir -p /var/opt/mssql/backup
  docker cp "$BAK_FILE" "$CONTAINER":/var/opt/mssql/backup/"$BAK_FILE"
}

echo "==> Restoring WideWorldImporters database..."
docker exec "$CONTAINER" /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$SA_PASSWORD" -C -Q "
RESTORE DATABASE WideWorldImporters
FROM DISK = N'/var/opt/mssql/backup/${BAK_FILE}'
WITH MOVE 'WWI_Primary' TO '/var/opt/mssql/data/WideWorldImporters.mdf',
     MOVE 'WWI_UserData' TO '/var/opt/mssql/data/WideWorldImporters_UserData.ndf',
     MOVE 'WWI_Log' TO '/var/opt/mssql/data/WideWorldImporters.ldf',
     MOVE 'WWI_InMemory_Data_1' TO '/var/opt/mssql/data/WideWorldImporters_InMemory_Data_1',
     REPLACE, STATS = 10;
"

echo "==> Sanity check: table count"
docker exec "$CONTAINER" /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$SA_PASSWORD" -C -d WideWorldImporters -Q "SELECT COUNT(*) AS table_count FROM sys.tables;"

echo "==> Done. WideWorldImporters is restored and ready."
