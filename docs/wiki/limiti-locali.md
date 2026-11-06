# Limiti locali e contesto

Per Codex/Continue, `codex-broker/server.mjs` accetta un body JSON fino a 32 MiB e `parseChatRequest()` fino a 1.000 messaggi e 2.000.000 caratteri testuali. Il parser continua a validare JSON, ruoli, modello, reasoning e immagini. Claude e Antigravity mantengono i limiti precedenti: body predefinito di 1 MiB, 200 messaggi e 750.000 caratteri.

Questi sono limiti del broker, separati da quelli di un eventuale reverse
proxy. Restano inoltre i limiti del modello, la memoria disponibile e i
timeout del provider e del gateway. La compattazione opzionale e i limiti
per chiave sono descritti in [Limiti token](limiti-token.md).

Il gateway limita anche i corpi HTTP prima del parsing JSON o multipart,
comprese le richieste chunked: `MAX_REQUEST_BODY_BYTES` (32 MiB),
`REQUEST_BODY_TIMEOUT_SECONDS` (30 secondi per ricevere il corpo) e
`MAX_CONCURRENT_API_REQUESTS` (16 richieste API attive, incluse quelle SSE).
Sono parametri tecnici del server, distinti dai tetti token delle chiavi.
Saturazione: HTTP 429 `gateway_busy` con `Retry-After`; corpo eccessivo:
413 `request_body_too_large`; ricezione lenta: 408 `request_body_timeout`.
Gli upload richiedono Content-Length e applicano anche il limite file più
l'overhead multipart. Non vengono restituiti contenuti del corpo negli errori.

Il catalogo Ollama distingue `reported_tokens` (finestra dichiarata dal
modello), `configured_tokens` (richiesta dal gateway) e `loaded_tokens`
(riportata da `/api/ps`, se il modello è caricato). Le capacità native sono
separate da quelle implementate dal gateway: la dichiarazione `tools` del
modello non abilita da sola i tool client nell'adattatore.
Ollama può normalizzare la finestra richiesta, ad esempio imponendo un
minimo ai modelli vision o limitandola alla finestra dichiarata; vedi
[scheduler ufficiale 0.40.14](https://github.com/ollama/ollama/blob/v0.40.14/server/sched.go).
Il controllo gateway è conservativo e resta basato su stime del tokenizer.
Un modello che dichiara una finestra inferiore viene rifiutato prima della
generazione quando la richiesta non rientra: configurare un valore locale
compatibile per consentire anche la sintesi preventiva su quel modello.

`scripts.verify_ollama_context` ha verificato su `qwen3.5:2b` la finestra
caricata di 4096 token, il rifiuto con chiave illimitata e la sintesi reale
per il tetto del motore, preservando i valori originali della chiave.

Una proposta di rimozione illimitata dei limiti condivisi è stata respinta dal controllo automatico per rischio di esaurimento delle risorse. L'aumento circoscritto a Codex è verificato in `provider-common/runtime.test.mjs`; ulteriori aumenti richiedono misura di memoria e timeout con richieste reali.
