# Installazione, backup, migrazione e ripristino

Personale: Compose e SQLite, un processo gateway. Team: autenticazione
amministrativa obbligatoria e identità locali, anche con SQLite. Organizzazione:
overlay `deploy/organization.yml`, PostgreSQL e repliche Linux sullo stesso host,
ingress HTTPS privato gestito dall'operatore. Vedi [team e organizzazioni](team-organizzazioni.md).

## SQLite

Il comando di backup usa l'API SQLite per una copia coerente anche con WAL
attivo, crea soltanto un file nuovo `0600` e verifica integrità e foreign key:

```bash
python -m scripts.backup_sqlite backup --source /data/omni_proxy.sqlite3 --output /data/backups/nuovo.sqlite3
python -m scripts.backup_sqlite restore --source /data/backups/nuovo.sqlite3 --output /data/ripristino.sqlite3
```

La destinazione non viene mai sovrascritta. Prima di adottare il ripristino,
fermare il gateway e scegliere esplicitamente il nuovo file. Conservare anche
vault, sessioni dei broker, workspace e journal sul volume `/data`: il solo DB
non include quelle credenziali o i file repository. Dopo il ripristino accessi
e revoche tornano allo stato del backup. Job in attesa di approvazione restano
in attesa; analisi interrotte possono ripartire dopo la scadenza del lease;
una scrittura interrotta viene recuperata dal journal senza rieseguirla alla cieca.

## Migrazione a PostgreSQL

Partire da un backup SQLite standalone, senza file `-wal`. Il comando di
ispezione mostra hash e conteggi, senza dati o segreti:

```bash
python -m scripts.migrate_postgres --source /data/backups/snapshot.sqlite3
```

Impostare `OMNIPROXY_TARGET_DATABASE_URL` nel solo ambiente del processo e
rieseguire con `--approve-sha256 HASH_VERIFICATO`. Il DB di destinazione deve
essere vuoto e senza gateway attivi. Lo schema viene creato, i dati copiati
in ordine di dipendenza, confrontati per hash di riga e le sequence aggiornate.
Errori o destinazione non vuota annullano la transazione dei dati. La sorgente
rimane intatta. Il registro `schema_versions` descrive la versione corrente
della destinazione; non viene retrocesso copiando la versione della sorgente.
La migrazione inversa PostgreSQL → SQLite non è implementata.

## PostgreSQL e upgrade

Eseguire `pg_dump --format=custom` con un account autorizzato e file di output
privato; provare `pg_restore --no-owner` in un database separato posseduto dal
ruolo runtime prima di adottarlo. Non mettere password negli argomenti dei
processi: usare ambiente o file credenziali privato. Il test riproducibile
`python -m scripts.verify_postgres` crea database effimeri e verifica migrazione,
restore, hash, accessi, stato job, eventi e sequence con PostgreSQL reale.

Prima di ogni upgrade: backup DB e volumi, immagine precedente identificata,
prova su copia, suite e smoke del container non privilegiato/read-only.
Ricostruire e ricreare solo i servizi interessati. Per rollback dopo una
migrazione, usare immagine **e backup compatibili**: il ritorno al solo binario
precedente non è una procedura di downgrade dello schema. Il gateway rifiuta
versioni schema più recenti di quelle che conosce. Conservare le prove e gli
eventuali consumi effettuati dopo il backup prima di ripristinare.

I gate funzionali non sono SLO di produzione. La concorrenza critica è
serializzata in DB; misurare il carico effettivo prima di scegliere numero di
repliche e obiettivi p95/p99. I limiti fisici dei modelli e le quote upstream
restano distinti dai limiti amministrativi delle chiavi.
