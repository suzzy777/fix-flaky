while IFS= read -r line; do [ -z "$(echo "$line" | cut -d, -f6)" ] && [ -z "$(echo "$line" | cut -d, -f7)" ] && echo "$line"; done < pr-data_june2026.csv > unfixed.csv


cut -d, -f5 unfixed.csv | sort | uniq -c | sort -k2 > breadown_unfixed.txt

awk -F, '$5=="ID"' unfixed.csv > ID_unfixed.csv
awk -F, '$5=="NIO"' unfixed.csv > NIO_unfixed.csv
awk -F, '$5=="TD"' unfixed.csv > TD_unfixed.csv
awk -F, '$5=="OD" || $5=="OD-Vic" || $5=="OD-Brit"' unfixed.csv > OD_unfixed.csv

awk -F, '{key=$1","$2","$3; rows[key]=$0} END {for (k in rows) print rows[k]}' ID_unfixed.csv | shuf -n 5 > ID_random_5.csv
awk -F, '{key=$1","$2","$3; rows[key]=$0} END {for (k in rows) print rows[k]}' OD_unfixed.csv | shuf -n 5 > OD_random_5.csv
awk -F, '{key=$1","$2","$3; rows[key]=$0} END {for (k in rows) print rows[k]}' NIO_unfixed.csv | shuf -n 5 > NIO_random_5.csv
awk -F, '{key=$1","$2","$3; rows[key]=$0} END {for (k in rows) print rows[k]}' TD_unfixed.csv | shuf -n 5 > TD_random_5.csv