mkdir -p /d/property-scout-db_backup
#
docker exec oncoord-postgres pg_dump \
  -U oncoord \
  -d property-scout \
  -Fc \
  > "/d/property-scout-db_backup/property-scout-$(date +%Y-%m-%d).dump"