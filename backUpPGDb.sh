mkdir -p /d/property-scout-db_backup
#
docker exec oncoord-postgres pg_dump \
  -U oncoord \
  -d property-scout \
  -Fc \
  > "/d/property-scout-db_backup/property-scout-$(date +%Y-%m-%d).dump"
  
  
#scp -i c:/Users/thale/.ssh/pelias_id_rsa \
#d:/property-scout-db_backup/property-scout-$(date +%Y-%m-%d).dump \
#oncoordadmin@4.151.234.176:/data/property-scout

#scp -i c:/Users/thale/.ssh/pelias_id_rsa \
#d:/property-scout-db_backup/property-scout-2026-10-06.dump \
#oncoordadmin@4.151.234.176:/data/property-scout

docker exec -i oncoord-postgres pg_restore \
  -U oncoord -d oncoord-db \
  --no-owner --no-privileges \
  --clean --if-exists --verbose \
  < /data/property-scout/property-scout-2026-10-06.dump
  
docker exec -it oncoord-postgres   psql -U oncoord -d oncoord-db   -c "SELECT current_database() AS database_name, relname AS table_name, n_live_tup AS estimated_rows FROM pg_stat_user_tables where relname in ('property_values','listings','gap_results') ORDER BY relname;"

  
#docker exec oncoord-postgres vacuumdb -U oncoord -d oncoord-db --analyze-only --verbose 
  
#psql -h 127.0.0.1 -p 5432 -U oncoord -d oncoord-db
