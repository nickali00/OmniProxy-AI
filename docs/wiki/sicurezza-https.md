# Sicurezza e HTTPS locale

Il profilo sicuro di `1.0.0-rc.3` usa Caddy e una CA privata per la LAN.
Questa pagina è una guida pubblica: indirizzi, impronte, scadenze effettive,
identificativi e backup dell'installazione restano nel runbook privato.
Gli esempi usano `192.0.2.10`, indirizzo riservato alla documentazione:
sostituirlo con l'indirizzo effettivo del proprio server.

## Confine di fiducia

Browser e applicazioni usano HTTPS, con base API `https://192.0.2.10/v1`.
Caddy termina TLS 1.2/1.3 e rifiuta 1.0/1.1; il gateway riceve soltanto su
socket Unix condiviso con il proxy, senza pubblicare una porta TCP.
Il proxy non privilegiato ha filesystem read-only, capability rimosse,
limiti di risorse, rete dedicata e API amministrativa disabilitata.
La porta HTTP precedente reindirizza le pagine e risponde 426 alle API.
Aggiornare l'URL dei client: un redirect non protegge una chiave già inviata
in chiaro.

`DASHBOARD_AUTH_REQUIRED=true` protegge dashboard e `/api/*` con l'identità
operatore preservando le chiavi applicative personali. `/admin/v1` resta
autenticato. Cookie Secure, HttpOnly e SameSite Strict, controlli Origin/header,
throttling e allowlist degli Host proteggono l'amministrazione. Gli header
proxy sono accettati soltanto nel profilo su socket Unix e riscritti da Caddy.
Gli upstream pubblici richiedono HTTPS nel profilo sicuro; PostgreSQL remoto
richiede `sslmode=verify-full`. Le connessioni Docker locali restano nel
confine dell'host fidato.

## Preparazione

Creare il profilo in una directory nuova e privata, senza modificare `.env`
o le sessioni dei provider:

```bash
.venv/bin/python -m scripts.prepare_https --directory .security \
  --host 192.0.2.10 --bind-address 192.0.2.10
docker compose -f docker-compose.yml -f .security/https.override.yml config --quiet
```

Verificare che `--tls-subnet` non si sovrapponga alle reti esistenti.
Inizializzare soltanto i tre volumi dedicati con `scripts.init_security_volumes`:
TLS e ingress UID/GID 10001, directory 0700, controllo 0750, segreto runner
0640. Il runner usa l'UID/GID del proprietario e i gruppi Docker/controllo;
comandi e letture falliscono chiusi senza la credenziale.
Prima di attivare: backup coerente, immagine precedente conservata,
migrazione su copia, smoke e provisioning locale con `scripts.provision_identity`.
Il token viene scritto nel file privato `--output`, mai nella documentazione.

Adottare l'override come `docker-compose.override.yml` soltanto dopo i gate.
Compose lo riutilizza durante gli aggiornamenti. Non eseguire `down -v`:
perdere la CA invalida la fiducia dei client. Conservare privatamente CA,
controllo, DB e vault; il rollback richiede immagine e dati compatibili.
`.security/`, override, certificati, credenziali e backup sono esclusi da Git.

## Fiducia sui client

Esportare soltanto il certificato pubblico della CA. Verificare hash del file
e impronta DER attraverso un canale fidato, prima di importarlo. Non esportare
chiavi private. Una VPN consente il collegamento ma non installa la CA.

Su Windows, dopo la verifica del file:

```powershell
certutil -hashfile "$env:USERPROFILE\Downloads\omniproxy-root.crt" SHA256
Import-Certificate -FilePath "$env:USERPROFILE\Downloads\omniproxy-root.crt" -CertStoreLocation Cert:\CurrentUser\Root
```

Container e runtime non ereditano il trust di Windows. Python richiede
`SSL_CERT_FILE` o un contesto SSL con `cafile`; Node può usare
`NODE_EXTRA_CA_CERTS` e un riavvio. Conservare chiavi e alias e aggiornare
soltanto URL e trust. Non usare `verify=False`, `curl -k` o
`NODE_TLS_REJECT_UNAUTHORIZED=0`.

Il login usa il token operatore, distinto dalle chiavi delle app. Il layout
ha una card e CSS esterno compatibile con CSP `style-src 'self'`.
L'indicatore gateway e la base API derivano dall'URL effettivo della richiesta,
con porta standard implicita e porte personalizzate conservate.

## Durate e promemoria

La PKI privata supporta un certificato server di 1826 giorni e un intermedio
di 2192 giorni; Caddy mantiene il rinnovo automatico. Queste durate riguardano
la LAN privata. Annotare privatamente scadenze e impronte effettive.
TLS storage è 0700 e le chiavi sono 0600; il marker di inizializzazione evita
che Docker sostituisca i permessi al primo mount vuoto.

Le sessioni ordinarie durano 1–168 ore. Per un'estensione locale esplicita
di una sessione operatore attiva, preservando lo stesso token:

```bash
docker compose exec -T gateway python -m scripts.extend_operator_session \
  --database /data/omni_proxy.sqlite3 \
  --credential-file /data/security/operator-session.json --years 5
```

La CLI accetta 1–5 anni, verifica token/identità/ruolo e registra l'audit.
Rifiuta sessioni scadute o revocate e identità inattive. Non esiste una nuova
rotta pubblica. Dopo la scadenza creare una nuova sessione della stessa
identità con `scripts.provision_identity --identity-id`. Il cookie può scadere
prima del token: riaprire `/admin/login` e reinserire lo stesso token valido.

`scripts.https_reminders` può funzionare su un host separato, usando soltanto
CA pubblica, configurazione privata Telegram e proprie ricevute SQLite.
Il modello Compose in `deploy/casa/compose.https-reminders.yaml` va adattato
alla propria immagine Python, rete, UID/GID e directory. Non avvia un poller
Telegram e non richiede credenziali dei provider AI.

Controlla ogni ora, con avvisi privati a 90/30/7/1 giorni e alla scadenza.
Il rinnovo aggiorna la scadenza osservata. Se il gateway è offline usa l'ultima
scadenza verificata senza notifiche ordinarie di indisponibilità.
`dashboard_sessions` contiene soltanto `session_id` ed `expires_at`, mai il
token; aggiornare/rimuovere i metadati dopo estensione, sostituzione o revoca.
Il monitor non autentica, rinnova o revoca il token. Le ricevute persistono
prima dell'invio: un timeout ambiguo resta `uncertain`, senza retry ciechi.
Occorrono host monitor acceso, Internet e bot autorizzato; non si promettono
consegna futura o lettura. `--once --dry-run` non invia messaggi.

## Verifiche

```bash
.venv/bin/pytest -q tests/test_security.py tests/test_identity.py tests/test_https_reminders.py tests/test_request_limits.py
.venv/bin/python -m scripts.verify_https --url https://192.0.2.10 \
  --ca-file .security/omniproxy-root.crt
```

Usare file privati `--api-key-file` e `--admin-session-file` per SSE/cookie,
una sessione di test con `--revoke-session` e un profilo sintetico da eliminare.
Non passare segreti negli argomenti della shell. `--http-origin` verifica
redirect pagine e rifiuto API HTTP.

Il gate HTTPS iniziale comprende 266 Python, 16 Node, doppia migrazione di
42 tabelle, smoke read-only, TLS e SDK OpenAI/Anthropic/Google con CA esplicita.
Il gate sessioni/promemoria comprende 283 Python. Le migrazioni client hanno
verificato applicazioni Python, Rust, robotica e n8n, chiavi e stati invariati,
chat/SSE, immagini, workflow reale e rifiuto TLS senza CA. Nessun comando
fisico ai robot. La prova non include installazione CA Windows né GUI browser.
I report pubblici sono minimizzati; il runbook completo resta privato.

pip-audit ha verificato 62 dipendenze runtime senza avvisi noti al momento
dell'audit; Bandit ha riportato 0 high, 27 medium e 250 low. Gli avvisi medi
riguardano anche SQL con identificatori vincolati, processi locali e healthcheck.
Le scansioni immagini conservano residui di sistema/vendor descritti sotto.
Questa revisione non è una certificazione indipendente o una garanzia di
assenza di vulnerabilità. Vedere i report in `spec/reports/`.

## Rischi residui da mantenere visibili

- I conteggi di Trivy includono librerie non esposte, codice vendorizzato e
  differenze tra severità NVD e classificazione Debian. Nessun conteggio va
  presentato automaticamente come numero di exploit riusciti o come zero rischi.
- `libxml2` del sistema mantiene CVE-2026-6653: Debian la classifica minor/no-dsa
  e non offre una patch stabile per il pacchetto presente. Parser, permessi e
  limiti riducono l'esposizione; non dimostrano l'assenza di ogni percorso.
  XML testuale/Office usa defusedxml. Gli avvisi nativi residui restano aperti.
- I broker contengono runtime e binari di terzi; la CLI Claude nativa non ha
  un account disponibile per una prova completa. Versioni e sessioni sono
  conservate. Gli strumenti npm inutilizzati vengono rimossi dal runtime.
- Zlib nei broker mantiene CVE-2023-45853 (`will_not_fix` Debian, codice
  minizip contrib: il rilevamento del pacchetto non ne dimostra l'esposizione).
  Caddy mantiene CVE-2026-116587 in go-jose/v3; il profilo usa l'emittente
  locale, senza ACME né autenticazione JWT, ma non si dichiara una prova
  formale di irraggiungibilità. La CLI Docker 29.9.3 mantiene
  CVE-2026-17106 e CVE-2026-78669 nelle dipendenze incorporate; monitorare
  un aggiornamento del binario ufficiale.
- Il runner Docker resta un componente privilegiato, ora autenticato e
  isolato. Gli amministratori Docker dell'host rimangono nel confine fidato.
- Le connessioni interne sullo stesso host a broker e Ollama sono HTTP su
  reti locali; non sono mTLS. Ollama è un servizio condiviso esposto anche
  sulla LAN: l'audit lo segnala, senza cambiare reti o firewall degli altri
  progetti. Per un confine ostile occorrono restrizioni di rete dedicate.
- I decoder multimediali sono subprocessi limitati nel container, non VM
  indipendenti. Separarli ulteriormente resta una misura aggiuntiva possibile.
- Non sono stati eseguiti attacchi distruttivi, saturazione di produzione,
  audit completo del sistema operativo host, installazione del certificato
  Windows o una verifica grafica del browser non disponibile alla sessione.

Fonti: [Caddy HTTPS locale](https://caddyserver.com/docs/automatic-https),
[durata dei certificati interni](https://caddyserver.com/docs/caddyfile/directives/tls#internal),
[durata dell'intermedio](https://caddyserver.com/docs/caddyfile/options#intermediate-lifetime),
[default SNI per accessi IP](https://caddyserver.com/docs/caddyfile/options#default-sni),
[Pillow 13, correzioni](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html),
[Cryptography](https://cryptography.io/en/latest/changelog/),
[Debian CVE-2026-6653](https://security-tracker.debian.org/tracker/CVE-2026-6653).
