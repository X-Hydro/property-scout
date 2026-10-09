mkdir -p /d/property-scout-db_backup
#
docker exec oncoord-postgres pg_dump \
  -U oncoord \
  -d property-scout \
  -Fc \
  > "/d/property-scout-db_backup/property-scout-$(date +%Y-%m-%d).dump"
  
  
scp -i c:/Users/thale/.ssh/pelias_id_rsa \
d:/property-scout-db_backup/property-scout-$(date +%Y-%m-%d).dump \
oncoordadmin@4.151.234.176:/data/property-scout
