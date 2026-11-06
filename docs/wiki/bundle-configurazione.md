# Bundle di configurazione

La CLI locale esporta profili, limiti token, Skills e riferimenti agli
strumenti MCP. Il formato JSON `omniproxy.configuration`, versione 1, usa
nomi per i riferimenti: gli ID numerici del database non sono portabili.

Non esporta chiavi API, hash, hint delle chiavi, credenziali MCP/OAuth,
sessioni dei broker, variabili ambiente, file allegati, conversazioni, job,
workspace o concessioni ai percorsi host. Gli URL MCP con credenziali, query
o frammenti vengono rifiutati. Il bundle conserva i testi delle Skills:
è un file privato di configurazione, non un contenuto da pubblicare.

```bash
.venv/bin/python -m app.config_bundles export --database /percorso/gateway.sqlite3 --output /percorso/bundle.json
.venv/bin/python -m app.config_bundles check --input /percorso/bundle.json
.venv/bin/python -m app.config_bundles import --database /percorso/gateway.sqlite3 --input /percorso/bundle.json --approval-hash HASH
```

I percorsi sono espliciti: la CLI non legge il database della configurazione
ambiente implicitamente. L'export apre SQLite in sola lettura e crea il file
con permessi 0600, senza sovrascrivere file o symlink. `check` restituisce
conteggi e hash del contenuto normalizzato, senza stampare testi o segreti.

L'importazione richiede l'hash esatto approvato e salva atomicamente il
bundle nel registro separato `configuration_templates`. È idempotente per
hash e rimane **inerte**: non crea chiavi, profili attivi, connessioni MCP,
Skills abilitate o accessi a cartelle. I template possono essere consultati
dal registro e applicati successivamente mediante le normali operazioni di
configurazione. Non esiste un endpoint HTTP di export/import. Attivazione
massiva e interfaccia amministrativa richiedono l'autenticazione e i ruoli
previsti dalla tappa team; non sono dichiarati disponibili qui.

`tests/test_config_bundles.py` prova roundtrip, esclusione dei segreti
gestiti, limiti, riferimenti, file privati, revisione, idempotenza e assenza
di modifiche agli accessi attivi.
