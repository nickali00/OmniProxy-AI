# Contesto e budget per chiave

Ogni profilo API può configurare `limits`: `input_tokens` (token di ingresso),
`context_tokens` (input più spazio
per l'output), `output_tokens` per chiamata, `context_mode` (`reject` oppure
`summarize`), `token_budget` e `budget_period` (`daily`, `weekly`, `monthly`,
`unlimited`). Il valore `null` e la scelta esplicita «Senza limite del gateway»
disabilitano il singolo tetto della chiave, mantenendo gli eventuali altri
tetti e i limiti fisici del modello. La dashboard permette sia creazione
sia modifica. Un aggiornamento che omette `limits` conserva quelli esistenti.

I budget includono input, definizioni e cronologia tool, output, riassunti e
ogni iterazione MCP. Anche la generazione dei job usa la stessa chiave e lo
stesso budget. Sono **token stimati dal gateway**, non dati di fatturazione
del provider: tokenizer, immagini e media possono avere costi differenti.
Il controllo dell'output limita ciò che il gateway restituisce; non può
annullare token che un provider abbia già generato oltre la richiesta.

Ollama riceve esplicitamente `num_ctx = OLLAMA_CONTEXT_TOKENS` (4096 per
default). La preparazione e la sintesi rispettano anche questo tetto locale,
senza cambiare i valori salvati sulla chiave. Se manca un massimo output,
la prenotazione predefinita è il minore tra `DEFAULT_BUDGET_OUTPUT_TOKENS` e
un quarto della finestra configurata. Il client può chiedere esplicitamente
più output quando input e output rientrano nella finestra. Un contesto troppo
grande per il modello usa `model_context_limit_exceeded` e l'header
`X-OmniProxy-Model-Context` identifica il tetto locale configurato.

I periodi seguono il calendario nel fuso `TOKEN_BUDGET_TIMEZONE` (predefinito
`Europe/Rome`): giorno a mezzanotte, settimana da lunedì, mese dal primo.
`GET /v1/usage` restituisce limiti, consumo, prenotazioni, residuo e rinnovo
della sola chiave autenticata. La dashboard mostra consumo e data di rinnovo.
Senza limiti significa nessun tetto di consumo imposto dalla chiave.
Il registro conserva i consumi anche passando tra periodi diversi o tornando
da `unlimited` a un budget. Il cambio di periodo non azzera il consumo già
effettuato nel nuovo intervallo. Le inferenze sono attribuite al momento
della prenotazione; i log HTTP aggregati non vengono conteggiati due volte.

Esempio di `limits` per un profilo: ingresso massimo 32.000, uscita massima
4.000, nessun tetto totale aggiuntivo e budget mensile di un milione di token:

```json
{"input_tokens":32000,"output_tokens":4000,"context_tokens":null,
 "context_mode":"reject","token_budget":1000000,"budget_period":"monthly"}
```

Per rimuovere tutti i tetti della chiave: i tre campi token e `token_budget`
a `null`, `budget_period: "unlimited"`, `context_mode: "reject"`. Un client
può sempre chiedere un output inferiore con `max_completion_tokens`;
chiederne uno superiore non oltrepassa il limite impostato sulla chiave.

## Concorrenza e arresti

Prima di ogni inferenza viene prenotato atomicamente input più output
massimo. Se il residuo è inferiore, l'output può essere ridotto; se non resta
spazio dopo l'input, la richiesta riceve HTTP 429 `token_budget_exceeded`
con `Retry-After`. A risposta conclusa viene contabilizzato il consumo
stimato effettivo e liberata la parte inutilizzata. Una disconnessione
contabilizza input e output già ricevuto. Prenotazioni rimaste senza esito
dopo un arresto vengono addebitate conservativamente per intero alla
scadenza. I limiti non possono cambiare mentre esistono richieste prenotate.

Gli header `X-OmniProxy-Budget-Remaining`, `X-OmniProxy-Budget-Reset` e
`X-OmniProxy-Tokens-Estimated` descrivono il budget. Nello streaming il
residuo negli header è una fotografia all'avvio; il consumo finale è
nell'evento usage e in `/v1/usage`. I limiti valgono anche per Responses,
Messages e generateContent. L'output fermato dal gateway usa `length`,
`incomplete` o l'equivalente del protocollo.

## Compattazione

La modalità predefinita rifiuta un contesto eccessivo con HTTP 400
`context_limit_exceeded`. La modalità `summarize`, attivata esplicitamente,
riassume i turni completi più vecchi, conserva i messaggi system/developer,
le definizioni tool e gli ultimi turni completi. Le coppie assistant tool
call / risultato non vengono separate. La sintesi usa un percorso senza
tool e non riesegue azioni. I riassunti possono perdere dettagli e consumano
token dello stesso budget.

`X-OmniProxy-Context-Compactions` indica il numero di chiamate di sintesi.
Con Responses e `store: true` viene salvato il contesto compattato per le
continuazioni. Nei client Chat Completions la cronologia appartiene al
client: se reinvia ogni volta il testo completo, può richiedere nuove
sintesi. Non viene troncato il messaggio corrente per farlo rientrare.
Le vecchie immagini non vengono trasformate in una sintesi testuale
automatica: quel caso restituisce un errore esplicito finché non esiste un
percorso di sintesi visiva verificato.

La modalità `summarize` richiede almeno un limite di ingresso o di contesto.
`scripts.verify_limits` ha verificato con Ollama `qwen3.5:2b` reale rifiuto
input, tetto output, sintesi della cronologia e conteggio delle sintesi nel
budget. I test automatici coprono anche isolamento delle chiavi, persistenza
al riavvio, cambio periodo e concorrenza delle prenotazioni.
