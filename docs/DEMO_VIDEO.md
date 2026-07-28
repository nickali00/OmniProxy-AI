# Video dimostrativo di OmniProxy AI

Questa guida serve a registrare una demo breve, comprensibile e sicura da
collegare al README del repository.

## Prima della registrazione

1. Usare un ambiente demo senza dati sensibili.
2. Chiudere terminali, notifiche, password manager e schede non pertinenti.
3. Non mostrare il contenuto di `.env`, i volumi dei provider o i codici OAuth.
4. Quando viene creata una API, oscurare la parte centrale della chiave.
5. Usare prompt di esempio che non contengano dati personali o aziendali.
6. Impostare la dashboard nella lingua usata durante la spiegazione.
7. Verificare che tutti i container necessari siano `healthy`.

## Storyboard consigliato — 4/6 minuti

### 0:00 — Il problema

Spiegare in una frase:

> Le mie applicazioni non devono conoscere ogni provider. Parlano con un solo
> endpoint OpenAI-compatible e OmniProxy sceglie modello e instradamento.

### 0:20 — Architettura

Mostrare il diagramma del README e indicare:

- FastAPI come gateway;
- SQLite per chiavi e consumi;
- Ollama per i modelli locali;
- sidecar isolati per i client cloud;
- n8n o un'altra applicazione come client.

### 0:50 — Avvio

Mostrare soltanto:

```bash
docker compose ps
curl http://127.0.0.1:8000/healthz
```

Evidenziare che la dashboard e il gateway sono limitati a localhost.

### 1:15 — Connessioni e lingue

Aprire la dashboard, cambiare lingua e mostrare:

- rilevamento automatico di Ollama;
- stato dei provider;
- collegamento tramite la pagina ufficiale del provider.

Non è necessario completare un nuovo login durante il video: si può mostrare
un account demo già collegato.

### 2:00 — Modelli

Aprire **Modelli**, selezionare un provider e mostrare che vengono elencati
soltanto i modelli realmente disponibili per quel provider.

### 2:30 — Creazione API

Creare una API gestita:

1. scegliere modello e reasoning;
2. assegnare un nome;
3. salvare;
4. oscurare la chiave prima che sia leggibile nel video;
5. mostrare lo slug stabile e il Base URL.

### 3:15 — Chiamata reale

Usare n8n o un comando `curl` con una chiave demo:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $OMNIPROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "demo-model-slug",
    "messages": [{"role": "user", "content": "Riassumi OmniProxy in tre punti"}]
  }'
```

### 4:15 — Consumi e quote

Aprire **Consumi** e mostrare:

- richieste effettuate;
- token in ingresso e uscita;
- latenza;
- provider e modello risolti;
- quota residua, quando disponibile dal client ufficiale.

### 5:00 — Chiusura

Riassumere:

> Un endpoint, chiavi locali separate, routing controllato e provider isolati.

Ricordare che Phase 1 non deve essere esposta direttamente su Internet senza
autenticazione amministrativa e TLS.

## Esportazione

- formato consigliato: MP4 H.264;
- risoluzione: 1920×1080;
- framerate: 30 fps;
- audio: voce chiara, senza musica troppo alta;
- durata ideale: meno di 6 minuti;
- evitare testo minuscolo: aumentare lo zoom del browser prima di registrare.

## Pubblicazione del video

Non inserire il file MP4 nella cronologia Git: rende il repository molto
pesante. Pubblicarlo invece come:

- video YouTube non in elenco o pubblico;
- allegato a una GitHub Release;
- file su un servizio di hosting video.

Poi sostituire nel README la frase “Il video sarà aggiunto dopo la
registrazione” con:

```markdown
[Guarda la demo completa](https://URL-DEL-VIDEO)
```

Per un'anteprima cliccabile, aggiungere un'immagine JPEG/WebP in
`docs/assets/` e usare:

```markdown
[![Demo OmniProxy AI](docs/assets/demo-cover.webp)](https://URL-DEL-VIDEO)
```
