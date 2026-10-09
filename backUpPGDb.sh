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
  --clean --if-exists \
  < /data/property-scout/property-scout-2026-10-06.dump
  
#docker exec oncoord-postgres vacuumdb -U oncoord -d oncoord-db --analyze-only --verbose 
  
#psql -h 127.0.0.1 -p 5432 -U oncoord -d oncoord-db
