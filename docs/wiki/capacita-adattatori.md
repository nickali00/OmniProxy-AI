# Capacità degli adattatori

Questa matrice riguarda il checkout del 5 novembre 2026 e soltanto
`/v1/chat/completions`. Non estendere una capacità da un provider a tutti i
suoi modelli o da un endpoint a un altro.

Legenda: **V** = verificato su un percorso reale indicato sotto;
**I** = implementato o coperto da test simulati, senza prova reale completa;
**N** = non supportato e rifiutato; **U** = non ancora accertato.

| Provider | Testo | Immagini in ingresso | Tool del client | MCP gestiti | SSE | JSON vincolato | Media nativi del provider |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Ollama | V | V, `qwen3-vl:latest` | N | I | V, progressivo | N | N |
| Codex | V | V, `gpt-5.6-sol` | V, `gpt-5.6-sol` | V, server sintetico MCP 2.x | V per tool; testo differito | N | N |
| Gemini/Antigravity | V | N | N | I | V, testo differito | N | N |
| Claude | I | N | N | I | I, testo differito | N | N |
| Upstream OpenAI compatibile | V, Ollama via `/v1` | V con opt-in deployment, `qwen3-vl` | V, `qwen3.5:2b` | I | V, Ollama via `/v1` | N | N |

Il gateway aggiunge un livello distinto di [allegati](allegati-multimodali.md):
estrazione documenti, OCR, immagini/pagine, video campionato e trascrizione
audio. Non è supporto nativo del provider a PDF, video o audio. Immagini e PDF
scansionati sono verificati con Codex; la trascrizione CPU locale e la chat
che la comprende sono verificate con Ollama. Testo estratto può essere usato
dai modelli testuali; pagine e fotogrammi richiedono capacità vision.

I tool del client sono eseguiti dal client, che rinvia il messaggio
`role: tool`. Gli strumenti MCP sono eseguiti dal gateway soltanto se
autorizzati nel profilo API. Le due modalità non possono essere combinate
nella stessa richiesta. Il supporto MCP qui indica l'orchestrazione del
gateway tramite istruzioni e parsing JSON, non tool calling nativo del modello.

## Prove registrate

- Ollama `qwen3.5:9b`: richiesta testuale e SSE reali tramite il gateway;
  HTTP 200, risposta attesa e terminatore `[DONE]`.
- Codex `gpt-5.6-sol`: Continue CLI 1.5.47 ha letto un file temporaneo con un
  tool e restituito il marker atteso. Una prova separata ha verificato
  `tool_calls → role: tool → risposta finale` sia normale sia SSE, con account
  autenticato e profilo API temporaneo poi eliminato.
- Test automatici: traduzione di `image_url` verso Ollama e Codex, rifiuto
  delle immagini nei broker Claude/Gemini, Skills e MCP tramite provider
  simulati. Questi test non certificano input visivi reali sui modelli cloud.
- L'adattatore upstream configurabile dal server è testato con risposte HTTP
  e SSE simulate e direttamente contro l'endpoint `/v1` di Ollama 0.40.14,
  modello `qwen3.5:2b`: testo e streaming hanno restituito contenuto. L'alias
  `upstream` compare soltanto se URL base e modello sono configurati. Il
  percorso completo attraverso l'app ASGI con DB temporaneo ha restituito
  HTTP 200 sia normale sia SSE; il container aggiornato non è stato ancora
  distribuito.
- L'upstream Ollama `qwen3.5:2b` ha restituito una function call reale; il
  client ha reinviato un risultato `role: tool` e il modello ha prodotto la
  risposta finale con il marker atteso. Nessun tool è stato eseguito dal
  gateway durante la prova diretta dell'adattatore.

## Rifiuti espliciti

- `response_format`, audio in uscita e modalità di output diverse dal testo
  restituiscono `validation_error` (HTTP 400). Parti `input_audio` o tipi
  sconosciuti nei messaggi non vengono ignorati silenziosamente. La risposta
  di validazione non ripete il contenuto della richiesta.
- URL remoti per `image_url` sono rifiutati; sono ammessi solo data URL nei
  formati e limiti descritti nel README.
- Ollama controlla `/api/show` prima di inviare un'immagine e restituisce
  `image_input_not_supported` se il modello non dichiara `vision`.
- I broker Claude e Gemini restituiscono `multimodal_not_supported` per le
  immagini. I tool del client fuori dai percorsi Codex e upstream restituiscono
  `client_tools_not_supported`.

## Verifiche ancora necessarie

1. Protocollo testuale legacy della GUI Continue; il percorso nativo del vero
   extension host 2.0.0 è verificato su VS Code Linux.
2. Ulteriori formati e dimensioni di immagini sui modelli dichiarati.
3. Claude Code nativo: account non disponibile. Antigravity CLI 1.5.1 è
   verificato con Gemini `gemini-3.8-flash-medium` e Claude
   `claude-sonnet-4-6`, normale e SSE tramite gateway con DB temporaneo.
4. Annullamento, concorrenza e limiti di contesto con carichi riproducibili.
5. Output strutturato e audio in tempo reale restano non supportati.
   [Sintesi vocale](audio-uscita.md) e [immagini generate](immagini-uscita.md)
   hanno endpoint e contratti separati dalla chat.

La baseline riproducibile `scripts/verify_baseline.py` ha verificato immagini
reali (identificazione del colore rosso) sia su `qwen3-vl:latest` sia su Codex
`gpt-5.6-sol`, tramite il gateway sorgente con DB temporaneo. I tempi e il
picco RSS del processo sono in `baseline-2026-11-05.json`; non sono SLO né
misure isolate del solo overhead del gateway. I test `test_disconnect.py`
usano socket HTTP reali per dimostrare che la disconnessione del client
chiude la richiesta al broker, normale/SSE e con/senza strumenti.
