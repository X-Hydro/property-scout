mkdir -p /d/property-scout-db_backup
#
  
docker exec oncoord-postgres pg_dump \
  -U oncoord \
  -d property-scout \
  -Fc \
  -t listings -t property_values -t gap_results \
  > "/d/property-scout-db_backup/property-scout-$(date +%Y-%m-%d).dump"
  
docker exec oncoord-postgres pg_dump \
  -U oncoord \
  -d oncoord-db \
  -n accounts \
  -Fc \
  > "/d/property-scout-db_backup/oncoord-db-accounts-$(date +%Y-%m-%d).dump"
  

scp -i c:/Users/thale/.ssh/pelias_id_rsa \
d:/property-scout-db_backup/property-scout-$(date +%Y-%m-%d).dump \
oncoordadmin@4.151.234.176:/data/property-scout

#scp -i c:/Users/thale/.ssh/pelias_id_rsa \
#d:/property-scout-db_backup/property-scout-2026-10-06.dump \
#oncoordadmin@4.151.234.176:/data/property-scout

docker exec -i oncoord-postgres pg_restore \
  -U oncoord -d property-scout \
  --no-owner --no-privileges \
  --clean --if-exists --verbose \
  < /data/property-scout/property-scout-current.dump
  
docker exec -i oncoord-postgres pg_restore \
  -U oncoord -d property-scout \
  --no-owner --no-privileges \
  --clean --if-exists --verbose \
  -t listings \
  -t property_values \
  -t gap_results \
  < /data/property-scout/property-scout-current.dump 


  
docker exec -i oncoord-postgres pg_restore -f - < /data/property-scout/property-scout-current.dump > /dev/null && echo OK 
  
#docker exec -it oncoord-postgres psql -U oncoord -d property-scout   -c "SELECT current_database() AS database_name, relname AS table_name, n_live_tup AS estimated_rows FROM pg_stat_user_tables where relname in ('property_values','listings','gap_results') ORDER BY relname;"

  
#docker exec oncoord-postgres vacuumdb -U oncoord -d property-scout --analyze-only --verbose 
  
#psql -h 127.0.0.1 -p 5432 -U oncoord -d property-scout

#curl -s "https://api.oncoord.com/property-scout/api/value-gap/rank?state=MA&limit=100&statuses=Active"


http://localhost:8080/property-scout/app/
https://www.oncoord.com/property-scout/app

#delete postgres from the vm
#docker rm -f oncoord-postgres
#sudo rm -rf /data/property-scout/postgres
#docker rmi postgis/postgis:16-3.4


         http://localhost:8061/api/value-gap/rank?state=RI&city=Bristol&statuses=Active%2CPending%2CContingent

curl -s "http://localhost:8061/api/value-gap/rank?state=NH&city=Lincoln&statuses=Active"