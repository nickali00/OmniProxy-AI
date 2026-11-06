# Contratto dei lavori gestiti — versione 1

Questo documento definisce il primo percorso agente della
[roadmap](roadmap.md). Il primo lavoro `repo_patch` è implementato nel
checkout: legge file autorizzati, propone una patch e può applicarla dopo
approvazione. I test isolati sono ora disponibili tramite un
[esecutore CLI autenticato](esecutori.md); ripresa esplicita, profili
riutilizzabili e retention configurabile sono implementati. Il workspace Build
sperimentale non soddisfa automaticamente questo contratto.

## Confini e identità

Un lavoro contiene `job_id`, proprietario, chiave applicativa, `profile_id`,
`workspace_id`, tipo `repo_patch`, istruzione, versione del contratto, stato,
timestamp e cursore eventi. La chiave autorizza il chiamante; il profilo
sceglie modello e permessi; il workspace identifica i file ammessi. Nessuna
chiave implica una memoria o un workspace condiviso per tutti i suoi lavori.

Il workspace è registrato prima del lavoro con un percorso canonico e una
politica di lettura/scrittura. Il client non può inserire un percorso host
arbitrario nella richiesta di avvio. L'esecutore rifiuta symlink, traversal,
mount inattesi, file segreti e percorsi esclusi. Lo snapshot registra hash e
dimensione dei file ammessi; la proposta conserva la base da cui è stata
generata.

## API

| Operazione | Semantica |
| --- | --- |
| `POST /v1/jobs` | Crea il lavoro con chiave idempotente; restituisce ID e stato `queued` |
| `GET /v1/jobs/{id}` | Stato, progressi, proposta e permessi del solo proprietario |
| `GET /v1/jobs/{id}/events` | SSE con sequenza monotona e ripresa da `Last-Event-ID` |
| `POST /v1/jobs/{id}/cancel` | Richiede annullamento idempotente; blocca nuove azioni |
| `POST /v1/jobs/{id}/approvals` | Approva o rifiuta una specifica proposta/hash; azione autenticata |
| `POST /v1/jobs/{id}/tests` | Accoda un profilo di test autorizzato su una specifica proposta/revisione |
| `GET /v1/jobs/{id}/tests` | Elenca gli esiti del proprietario, senza snapshot né token di claim |
| `GET /v1/jobs` | Elenco paginato di metadati, senza snapshot o contenuto delle patch |
| `POST /v1/jobs/{id}/resume` | Riprende un job annullato/fallito con nuova revisione e chiave idempotente |
| `DELETE /v1/jobs/{id}` | Elimina soltanto cronologia terminale sicura; non modifica i file |
| `/v1/job-profiles` | Crea/elenca profili immutabili della chiave, con workspace, istruzioni e percorsi |
| `POST /v1/job-profiles/{id}/jobs` | Avvia una nuova istanza del profilo con istruzione aggiuntiva opzionale |

`POST /api/workspaces`, protetto dalle regole amministrative della dashboard,
registra nome, radice, lista esplicita `paths`, `api_key_ids` autorizzati e
permesso `writable` (predefinito `false`). La radice deve appartenere a
`JOB_WORKSPACE_ROOTS`; la configurazione vuota disabilita la registrazione.
Non vengono montati percorsi host automaticamente. In Docker occorrono un
mount esplicito e permessi per UID 10001 sui soli file autorizzati.
`GET /v1/workspaces` restituisce soltanto i workspace concessi alla chiave,
senza il percorso host.

Creazione: chiave associata a un profilo API, header `Idempotency-Key`, corpo
`{"type":"repo_patch","workspace_id":"ws_...","instruction":"..."}` e
`paths` opzionale come sottoinsieme della lista autorizzata. Provider iniziali:
Ollama, Codex e upstream OpenAI compatibile; il turno Codex usa il percorso
app-server con tutti i tool disabilitati. I profili Claude/Gemini non sono
ancora abilitati per i job. La proposta contiene `hash`, `diff` e `changes`
con i contenuti completi dei file. L'approvazione richiede
`{"decision":"approve","proposal_hash":"..."}`. Il rifiuto può includere
`revised_instruction` per rigenerare; senza nuova istruzione annulla il job.
L'API non elimina file né crea directory.

Limiti iniziali: 50 file, 256 KB per file, 1 MB di snapshot, 16.000 caratteri
di istruzione, quattro job attivi per chiave (configurabile) e un job attivo
per workspace. Le scritture hanno un solo lock anche tra workspace
sovrapposti. SSE accetta `Last-Event-ID` e `follow=false` per il solo replay.

Le risposte usano errori stabili: `job_not_found`, `job_forbidden`,
`invalid_job_state`, `approval_required`, `stale_workspace`,
`tool_not_allowed`, `job_conflict` e `job_limit_exceeded`. Nessuna risposta
contiene token del provider o percorsi esterni al workspace.

## Stati e transizioni

```text
queued → running → awaiting_approval → applying → completed
             ↘ failed             ↘ failed
queued/running/awaiting_approval → cancelled
awaiting_approval → running (proposta rifiutata e nuova istruzione)
```

`cancel` è una richiesta di arresto: un'azione già conclusa resta registrata;
non si promette rollback di effetti esterni. Una proposta approvata porta
l'ID e l'hash della patch. La stessa approvazione non può applicare due volte
la patch. Prima di scrivere, l'esecutore ricontrolla hash, permessi e percorso
di tutti i file; se uno diverge, non scrive nulla e restituisce
`stale_workspace`. La scrittura è atomica dove possibile e ha un journal per
il recupero dopo errore.

Le nuove proposte hanno `hash_version: "2"`: l'hash lega contenuti proposti,
hash della base e revisione. Il client tratta l'hash come opaco e usa quello
restituito dal server. Le proposte già memorizzate mantengono il loro hash.
Ripresa e rigenerazione dopo rifiuto incrementano la revisione: una vecchia
approvazione non autorizza una nuova proposta anche se produce gli stessi byte.

La ripresa richiede `Idempotency-Key`, corpo `{}` oppure `instruction`, chiave
e profilo ancora validi e concessione corrente del workspace. Riparte soltanto
l'analisi in lettura, con snapshot nuovo; serve una nuova approvazione per
scrivere. Non riprende job completati o con `workspace_recovery_required`.
Sono ammessi al massimo 100 comandi di ripresa distinti per job. Replay dello
stesso comando restituisce lo stato corrente; contenuto diverso con la stessa
chiave è un conflitto. I token worker impediscono al processo precedente di
pubblicare o rimettere in coda il lavoro del nuovo proprietario.

I profili job sono immutabili e appartengono alla chiave: fino a 100 per chiave,
istruzione base fino a 12.000 caratteri, aggiunta fino a 4000, totale 16.000.
Non concedono percorsi, tool o account aggiuntivi. Ogni avvio ricontrolla i
permessi attuali; eliminare il profilo non cambia i job già creati, che ne
conservano ID e fingerprint. Nessuna regola di WikiNick è incorporata nel core.

Annullamento e rigenerazione annullano i test ancora in coda e segnalano
`cancel_requested` agli esecutori attivi. I test vecchi non vengono rieseguiti
automaticamente sulla nuova revisione. La cancellazione della cronologia
rifiuta job attivi, recuperi manuali pendenti e test ancora attivi. Consumo e
metadati di idempotenza restano: una richiesta di creazione già eliminata
restituisce 410 `job_history_expired`, anziché creare nuovamente il lavoro.

`JOBS_RETENTION_DAYS=0` conserva la cronologia. Un valore positivo abilita la
pulizia a blocchi dei soli job terminali sicuri oltre l'età indicata; non
cancella file repository, workspaces, profili o consumo. Il registro minimo
di idempotenza conserva soltanto ID, data e hash della chiave della richiesta.

## Proprietà dei tool

- **Proxy:** il client esegue i propri tool e rinvia `role: tool`.
- **MCP gestito:** il gateway chiama soltanto tool autorizzati nel profilo.
- **Lavoro agente:** l'esecutore del job usa i tool dichiarati dal tipo di
  lavoro e dai permessi del workspace. Un tool ha una sola modalità e un solo
  proprietario per richiesta; non si inoltra la stessa chiamata ad altri due.

L'analisi iniziale è in sola lettura. La proposta di patch non scrive file.
L'applicazione e i test sono azioni separate. I comandi di test provengono da
una allowlist del workspace, hanno timeout, limite output e permessi espliciti;
non accettano una shell arbitraria proposta dal modello. Output dei tool e
file del repository sono dati non fidati, non istruzioni di sistema.

## Persistenza e ripresa

Eventi, transizioni e approvazioni sono persistiti prima di comunicare il
successo al client. Il worker usa lease con scadenza e una transizione
atomica per acquisire un job. Dopo riavvio può riprendere sola lettura e
generazione di proposte; prima di ripetere scritture o comandi controlla il
journal e richiede revisione se l'esito precedente è incerto. L'ordine degli
eventi è stabile anche dopo riconnessione SSE. Retention e cancellazione dei
contenuti del job sono configurabili per profilo di distribuzione.

La prima implementazione usa un solo worker e SQLite. Con PostgreSQL e più
repliche restano invariati ID, transizioni e regole di idempotenza.

L'implementazione attuale salva snapshot, proposta e journal in SQLite. Non
salva contenuti negli eventi né nei log ordinari. La generazione interrotta
viene riaccodata; le applicazioni con lease scaduto vengono ripristinate solo
quando i file corrispondono agli hash del journal. Modifiche esterne
incompatibili producono `workspace_recovery_required`, senza sovrascriverle.
La retention automatica e PostgreSQL sono ancora pianificati.

## Evidenze del checkout

`tests/test_jobs.py` verifica proposta/approvazione, patch obsoleta senza
scritture, doppia approvazione, richieste concorrenti idempotenti,
annullamento, isolamento fra chiavi, symlink/hardlink/traversal, mount
inattesi, rollback dopo errore e ripresa del worker. Lo script
`scripts/verify_job.py` ha completato un job reale con Codex `gpt-5.6-sol`,
workspace e DB temporanei, proposta, approvazione, modifica, replay eventi e
riuso della stessa chiave idempotente. Il codice è incluso nel gateway
distribuito; i workspace richiedono comunque radici, mount e concessioni
esplicite. Nessun percorso host viene abilitato automaticamente.

## Verifiche minime prima del rilascio

1. Creazione, lettura, SSE, ripresa da cursore, annullamento e accesso negato
   tra chiavi o tenant diversi.
2. Proposta valida, approvazione, rifiuto, doppia approvazione e patch obsoleta
   senza scritture parziali.
3. Path traversal, symlink, file esclusi, tool non autorizzato, timeout e
   output eccessivo rifiutati.
4. Riavvio in ogni stato; nessun comando o patch ripetuto automaticamente.
5. Concorrenza, limiti per chiave e workspace, pulizia delle risorse e audit
   che non include prompt o credenziali per impostazione predefinita.
