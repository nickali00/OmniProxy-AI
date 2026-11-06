# Esecutori dei test

I test dei job usano un esecutore CLI avviato dall'operatore. Il gateway
mantiene coda, permessi, revisioni, lease, eventi e risultati. Non riceve
accesso a Docker e non viene aggiunto un servizio HTTP privilegiato.

La registrazione del workspace può autorizzare `test_profiles`:
`python-unittest` e `python-compile`; l'impostazione predefinita è vuota.
Non sono accettati comandi, immagini, argomenti o shell scelti dal modello.
La CLI usa un'immagine Python ufficiale fissata per digest, installata prima
dell'avvio. L'immagine necessaria è indicata in `app/test_sandbox.py`.
Non vengono scaricate immagini durante i job (`--pull never`).

## Richiesta e revisione

`POST /v1/jobs/{id}/tests`, con `Idempotency-Key`:

```json
{"profile":"python-unittest","proposal_hash":"HASH_DELLA_PROPOSTA",
 "target":"proposal"}
```

`proposal` verifica una copia della proposta prima di approvarla. `applied`
richiede un job completato e ricontrolla che i file attuali corrispondano
alla revisione approvata. I test vengono eseguiti solo sui file autorizzati,
senza scrivere nel workspace originale. I risultati sono riferiti alla
proposta indicata, non certificano modifiche successive del repository.
`GET /v1/jobs/{id}/tests` restituisce le ultime 100 esecuzioni del proprietario.

## Avvio della CLI

La CLI richiede Python, le dipendenze del repository e Docker. Impostare
`OMNIPROXY_API_KEY` nell'ambiente usando una chiave con concessione al workspace;
non inserirla negli argomenti o negli URL. Poi avviare:

```bash
.venv/bin/python -m scripts.run_executor --url http://127.0.0.1:8181 --once
```

`--watch` mantiene il processo in attesa di altri test della stessa chiave.
Il progetto e i test di questa installazione restano sul server Nicola.
La CLI non installa servizi, non salva la chiave e non apre porte in ingresso.
Il protocollo è utilizzabile anche da un esecutore Windows con motore Docker
Linux, ma il percorso Windows non è stato verificato e non è dichiarato
supportato nativamente.

## Isolamento e ripresa

Ogni test crea un container senza rete, con UID non privilegiato, root in
sola lettura, capability rimosse, no-new-privileges, limiti di CPU, memoria,
processi e tmpfs. Nessun percorso host, socket Docker o credenziale provider
viene montato. I file sono copiati in un tmpfs e rimossi con il container.
I risultati sono dati non fidati riferiti dall'esecutore autenticato.

Massimi: 50 file, 256 KB per file, 1 MB totale, due test attivi per chiave,
64 KiB di output, timeout 30 secondi configurabile fino a 300. Il lease è
60 secondi con heartbeat. API esecutore: `POST /v1/job-tests/claim`,
`/{id}/heartbeat`, `/{id}/result`, `/{id}/cancel`. Ogni operazione richiede
la chiave del proprietario; heartbeat e risultato richiedono inoltre il
token del claim, salvato nel DB soltanto come hash.

Un claim scaduto diventa `interrupted` e non viene ripetuto automaticamente.
Ripetere un risultato identico è idempotente. Per richiedere una nuova prova
serve una nuova chiave idempotente. Annullare un test attivo segnala la
richiesta alla CLI al successivo heartbeat, che termina il container.
Revoca della chiave o perdita di connessione arrestano la CLI e lasciano
scadere il claim, senza replay. Gli eventi non contengono snapshot o output.

`tests/test_job_tests.py` verifica permessi, claim concorrenti, idempotenza,
scadenza, annullamento, revisioni e limiti. `scripts.verify_job_tests` esegue
una proposta Codex reale, test isolati prima e dopo approvazione, controlli
effettivi di isolamento, timeout, annullamento e output eccessivo.
