# Verifiche e rilascio

Prima: `git status --short`, `docker compose config --quiet`, versione installata e diff dei file interessati. Non ripulire o ripristinare modifiche locali non proprie.

Test mirati:

Per HTTPS e promemoria: `pytest -q tests/test_security.py tests/test_https_reminders.py`.
Il controllo `scripts.https_reminders --config FILE --once --dry-run` non invia
Telegram. `--test-message --once` invia invece una conferma privata: usarlo
solo per una richiesta di notifiche autorizzata. Le ricevute incerte non
vanno eliminate o ripetute senza riconciliazione.

```bash
.venv/bin/pytest -q tests/test_continue_tools.py tests/test_gateway.py tests/test_responses_api.py tests/test_anthropic_api.py tests/test_gemini_api.py tests/test_upstream.py
node --test codex-broker/client-tools.test.mjs
node --check codex-broker/server.mjs
git diff --check
```

Per i job: `.venv/bin/pytest -q tests/test_jobs.py`. Per la propagazione
dell'annullamento su socket: `tests/test_disconnect.py`. La baseline reale
si esegue con `python -m scripts.verify_baseline`; il ciclo job reale con
`python -m scripts.verify_job --broker-url URL_INTERNO_BROKER`. Entrambi
usano DB e workspace temporanei. Il test IDE `scripts.verify_vscode` crea e
revoca un profilo temporaneo, avvia un test host VS Code con dati isolati e
non modifica la configurazione Continue dell'utente.

Per limiti e media: `tests/test_token_budget.py`, `tests/test_inference_limits.py`,
`tests/test_request_limits.py`, `tests/test_files.py`, `tests/test_audio.py`.
`python -m scripts.verify_limits` verifica input, output, compattazione e
budget con Ollama reale; `python -m scripts.verify_audio --model-path PATH`
prova trascrizione CPU e allegato audio fino alla chat locale. Tutti usano
database e dati sintetici temporanei. La suite completa è `.venv/bin/pytest -q`.

Per distribuire codice modificato serve `docker compose build` seguito da `docker compose up -d --no-deps --force-recreate` sui soli servizi interessati. Un semplice restart non incorpora i sorgenti. Verificare `docker compose ps`, `/healthz`, versione CLI nel broker e status API. Non esporre chiavi o volumi nei log.

Un test simulato del broker non sostituisce il ciclo reale con Continue e un account Codex collegato. Registrare esplicitamente versione distribuita e verifiche mancanti.

## Contratto pubblico e candidato 1.0

Per i [gruppi di chiavi](gruppi-chiavi.md):
`pytest -q tests/test_api_groups.py` verifica persistenza, spostamento,
eliminazione senza revoca e controlli amministrativi;
`node --test tests/dashboard-groups.test.cjs` verifica il renderer reale su
DOM minimo, apertura/chiusura ed errore di salvataggio. Non equivale a una
prova grafica nel browser. `scripts.verify_postgres` include ora CRUD,
migrazione e restore dei gruppi. `scripts.verify_container` prova i gruppi
nell'immagine read-only e controlla che la chiave resti valida.

`python -m scripts.export_openapi --check` confronta lo schema pubblico con
l'artefatto versionato, senza avviare storage o provider. La discovery
`GET /v1/capabilities` descrive configurazione e limiti della sola chiave;
non è una verifica di account o backend. Header del contratto/prodotto sono
presenti anche sugli errori di autenticazione. La [specifica](../../spec/README.md)
definisce sottoinsiemi ed esclusioni; non dichiara equivalenza totale alle API
dei vendor o adozione indipendente.

`conformance.check` è un client Python standard library senza import del
gateway. `conformance.sdk_checks` verifica OpenAI, Anthropic e Google Gen AI
con endpoint esplicito e chiave locale; dipendenze fissate in
`conformance/requirements.txt`, fuori dal runtime server. Usare chiavi dedicate:
le richieste consumano budget, creano soltanto dati sintetici e ripuliscono
le proprie risposte/file. La [guida](../../conformance/README.md) include comandi,
esempi, scope e report privi di contenuti/credenziali.

`scripts.verify_conformance` avvia il gateway su socket HTTP loopback e DB
temporaneo, azzera PostgreSQL/team/OTLP e lancia i runner come processi
separati contro Ollama reale. Non confondere questa prova con ASGI simulato.
La suite core copre testo/documenti; non certifica da sola job, amministrazione
o ogni provider. Le rispettive verifiche restano necessarie.

`scripts.verify_upgrade --source BACKUP` lavora esclusivamente su copia
temporanea, applica due volte le migrazioni e confronta per hash le righe
e colonne preesistenti, oltre a integrità/FK. Montare il volume originale
in sola lettura nel container di prova. Il registro versioni cambia
intenzionalmente e non viene confrontato come contenuto applicativo.

Il numero prodotto `1.0.0-rc.4` indica una release candidata, con
[politica di versione/supporto](../../spec/versioning.md) e
[processo RFC](../../spec/proposals/README.md). Pubblicazione remota, revisione
di terzi e adozione come standard richiedono evidenze esterne distinte.

## Pubblicazione senza dati privati

Prima del commit controllare i file effettivamente staged e la storia Git con
uno scanner di segreti. Verificare anche l'assenza delle credenziali locali
per confronto esatto, senza stamparle. `.env`, `.security/`, override,
certificati, DB, backup e checkout separati non devono entrare nel commit.
Rendere portabili esempi e script; nei report pubblici mantenere risultati
e limiti, omettendo percorsi personali, topologia, impronte e metadati delle
sessioni reali. I dati completi restano soltanto nel runbook privato.
Eseguire la suite completa, controlli Node, schema OpenAPI e Compose prima
del push. Verificare infine che il commit remoto coincida con quello locale.

La CI installa anche `poppler-utils`, Tesseract (eng/ita), `ffmpeg` ed
`espeak-ng`, come l'immagine gateway: i test multimediali eseguono veri
estrattori e sintetizzatori. Installare soltanto i requisiti Python non basta.

Il runner è fissato a Ubuntu 24.04, distribuzione della verifica nativa.
`ubuntu-latest` è passato a 26.04: con i pacchetti multimediali installati,
276 test passano ma sette prove di sintesi vocale falliscono nel worker
limitato. La sintesi su Ubuntu 26.04 resta non qualificata; non aumentare i
limiti del worker né saltare test soltanto per ottenere una CI verde.
