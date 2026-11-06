# Identità, team e organizzazioni

La modalità personale resta Compose + SQLite, un processo gateway, dashboard
protetta da Host/Origin e chiavi applicative. La modalità team richiede
`ADMIN_AUTH_REQUIRED=true`: dashboard e API amministrative legacy richiedono
un'identità operatore. Le nuove API `/admin/v1` richiedono sempre una sessione
personale, anche quando il server è personale. Se il database contiene tenant,
il riavvio con autenticazione amministrativa disattivata viene rifiutato.

## Oggetti e ruoli

| Oggetto | Responsabilità |
| --- | --- |
| Identità | Persona, attivazione e ruolo operatore dell'istanza |
| Sessione personale | Token distinto dalle chiavi AI, hash nel DB, emissione ordinaria 1–168 ore e revoca; estensione operatore locale esplicita di 1–5 anni |
| Tenant | Organizzazione e appartenenze con ruoli owner/admin/developer/viewer |
| Progetto | Ambito di profili e chiavi dentro un tenant |
| Profilo | Accesso pubblicato dall'operatore a modello, Skills e tool autorizzati |
| Chiave applicativa | Credenziale indipendente associata al profilo, con limiti e consumo propri |

Gli owner gestiscono anche gli amministratori; gli admin gestiscono progetti,
chiavi e limiti. Developer/viewer consultano i metadati autorizzati. L'ultimo
owner non può essere rimosso. L'operatore pubblica esplicitamente i profili:
un amministratore tenant non può acquisire accesso ad account, Skills o MCP
globali conoscendone l'ID. L'operatore appartiene ai tenant che crea; non
ottiene implicitamente tutte le appartenenze delle organizzazioni esistenti.
Le appartenenze al tenant sono ereditate dai suoi progetti.

Più chiavi possono usare lo stesso profilo con tetti input/output/contesto e
budget indipendenti. La sospensione del profilo/progetto nega nuove richieste
a tutte le chiavi associate; la revoca di una chiave lascia attive le altre.
Job, file e Responses restano vincolati alla chiave creatrice. Le chiavi
legacy senza ambito non sono accettate in modalità team e non vengono
adottate automaticamente. I permessi del workspace restano concessioni
distinte: pubblicare un profilo non concede accesso ai file.

## Provisioning e console

Prima di abilitare la modalità team, creare localmente l'identità operatore:

```bash
python -m scripts.provision_identity --database /data/omni_proxy.sqlite3 \
  --name Nicola --operator --output /data/operator-session.json
```

Usare `--postgres` invece di `--database` per il DB PostgreSQL configurato nel
processo. Il file deve essere nuovo ed è scritto `0600`; il token non viene
stampato. Per una sessione scaduta, `--identity-id usr_...` genera una nuova
sessione della stessa identità, conservando appartenenze e ruoli.

`scripts.extend_operator_session --database /data/omni_proxy.sqlite3
--credential-file /data/security/operator-session.json --years 5` estende
soltanto la sessione operatore attiva che corrisponde al token nel file
privato. Conserva token, hash e identificativo; rifiuta sessioni scadute,
revocate, identità inattive e non operatori. Non è esposto da una rotta HTTP
e non cambia la durata predefinita delle altre sessioni. Il limite è cinque
anni di calendario dall'operazione; il 29 febbraio viene riportato al 28
febbraio se necessario. L'operazione è transazionale e registrata nell'audit.
L'output contiene soltanto metadati e scadenza, mai il token. Le istruzioni
per i promemoria sono in [sicurezza e HTTPS](sicurezza-https.md).

Aprire `/admin/login` e inserire il token. Il browser usa cookie HttpOnly,
SameSite Strict e Secure su HTTPS; il token non viene salvato in localStorage.
`/admin` offre progetti, profili pubblicati, creazione/revoca chiavi e modifica
dei limiti. I membri sono amministrabili dalle API. Provisioning di nuove
identità e recupero delle sessioni restano operazioni locali dell'operatore.

API principali: `/admin/v1/me`, `/tenants`, `/tenants/{id}/members`,
`/tenants/{id}/projects`, `/projects/{id}/profiles`, `/projects/{id}/keys` e
`/projects/{id}/keys/{key}/limits`. I segreti delle chiavi compaiono solo alla
creazione, mai negli elenchi. Cookie e Bearer usano gli stessi ruoli; le
mutazioni con cookie richiedono Origin corrispondente e l'header dashboard.

## PostgreSQL e repliche

`DATABASE_URL` abilita PostgreSQL; vuoto mantiene SQLite. Il backend usa
transazioni e advisory lock DB per preservare l'atomicità del core anche
tra processi. Le scritture critiche restano serializzate: questo rilascio
verifica correttezza, non promette throughput lineare con il numero di repliche.
Il ruolo runtime dell'overlay organizzazione non è superuser e non può creare
database o ruoli. SQLite usa NOCASE ASCII; PostgreSQL usa ICU senza distinzione
di maiuscole: collisioni durante una migrazione fanno fallire e annullare
l'importazione, senza modificare il sorgente.

Per preparare un nuovo deployment:

```bash
python -m scripts.prepare_organization --directory /percorso/privato/nuova-organizzazione
docker compose -p omniproxy-organization --env-file /percorso/privato/nuova-organizzazione/organization.env \
  -f docker-compose.yml -f deploy/organization.yml config --quiet
```

L'overlay prepara PostgreSQL e due repliche senza pubblicare porte gateway.
L'ingress HTTPS va collegato dall'operatore alla rete privata; configurare
Host consentiti e proxy fidati. Non esporre PostgreSQL o i broker. Usare un
nome di rete distinto dall'istanza personale se convivono sullo stesso host.
Le credenziali ambiente sono accessibili agli amministratori Docker dell'host.

Le repliche verificate sono Linux sullo stesso host e devono condividere
`/data`, vault e gli stessi mount di workspace agli stessi percorsi: lock
filesystem e journal devono riferirsi agli stessi inode. I claim dei job
hanno token di proprietà; heartbeat, completamento e recupero di un worker
obsoleto non possono modificare un nuovo proprietario. I lease upstream e i
cooldown 429 sono condivisi nel DB con deadline totale; i broker CLI hanno
un proprio limite nel sidecar comune. Multi-host e filesystem distribuiti
non sono certificati da queste prove.

Verifiche: suite ruoli/confini/revoca/CSRF; PostgreSQL 17.11 reale senza porte
host, ruolo limitato, otto processi per claim e capacità, migrazione con hash
delle righe e dump/restore. Per operazioni e osservabilità vedere le pagine
[operazioni](operazioni.md) e [telemetria](osservabilita.md).
