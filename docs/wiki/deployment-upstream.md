# Deployment upstream multipli

`UPSTREAM_DEPLOYMENTS_JSON` configura al massimo 32 endpoint amministrati dal
server. Ogni deployment ha ID, URL base `/v1`, modello, eventuale `api_key`,
`max_concurrency` (predefinito 2), `capacity_group`, `fallbacks`,
`fallback_on` e opzione `images` (predefinita false). URL e credenziali non
possono essere impostati nei prompt o nelle richieste di inferenza.

Esempio locale senza segreti:

```json
[
 {"id":"primary","base_url":"http://model-a:8000/v1","model":"chat",
  "max_concurrency":2,"fallbacks":["secondary"],
  "fallback_on":["busy","connect_error","quota"]},
 {"id":"secondary","base_url":"http://model-b:8000/v1","model":"chat"}
]
```

Le chiavi upstream sono configurazione privata del server; non esportare
questa variabile in bundle pubblici. Il catalogo espone soltanto gli alias
`upstream:primary`, `upstream:secondary`, modelli e diagnostica priva di URL
e segreti. La dashboard permette di legare una chiave a uno di questi alias.
L'upstream singolo precedente conserva il proprio comportamento e alias.

## Concorrenza, quote e fallback

Il gateway non accoda richieste senza limite: a capacità esaurita restituisce
429 `deployment_busy`, oppure prova un fallback esplicitamente configurato.
Deployment che condividono un account possono indicare lo stesso
`capacity_group`: condividono concorrenza e cooldown, applicando il più basso
dei loro limiti. Sono controlli del singolo processo; il coordinamento tra
repliche richiede il backend condiviso previsto dalla tappa organizzazioni.

Un 429 upstream attiva un cooldown seguendo `Retry-After`, tra 1 e 600 secondi
(60 se assente o non numerico). I fallback sono una lista finita, senza retry
ricorsivi. Le sole cause ammesse sono saturazione locale, connessione non
stabilita e quota 429. Errori di lettura, risposte malformate, errori 5xx e
autenticazione non vengono riprovati automaticamente.

Il fallback è disabilitato per chiamate e cronologia tool del client. Nello
streaming si ferma definitivamente al primo contenuto del modello emesso;
gli eventi iniziali di protocollo possono precedere la selezione. Il gateway
non riesegue tool MCP già completati: la selezione avviene durante la sola
generazione del successivo messaggio.

Le risposte normali espongono `X-OmniProxy-Deployment` e
`X-OmniProxy-Fallback`. L'ultimo chunk Chat Completions SSE espone
`omniproxy: {deployment, model, fallback}`. Il catalogo amministrativo riporta
capacità attiva, tentativi, errori, fallback e cooldown. I contatori non sono
persistenti e non contengono prompt, chiavi o risposte dei provider.

## Immagini e verifiche

`images: true` abilita il formato OpenAI image_url per un deployment la cui
capacità vision sia stata verificata dall'amministratore. Non converte un
modello testuale in modello vision. Il percorso verso Ollama `/v1` con
`qwen3-vl:latest` ha superato una prova reale di riconoscimento del colore.

`tests/test_deployments.py` verifica credenziali distinte, catalogo, claim di
capacità, account condivisi, quota, annullamento, streaming e assenza di
replay dei tool. `scripts.verify_deployments` prova gateway → endpoint
irraggiungibile → Ollama reale, testo/SSE, rifiuto del retry dei tool e vision.
I costi restano stime del gateway, non fatturazione autorevole del provider.
