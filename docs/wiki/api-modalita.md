# API e modalità

## Proxy

Il client invia la cronologia completa. Se passa `tools`, OmniProxy restituisce una chiamata OpenAI `tool_calls`; il client esegue lo strumento e reinvia un messaggio `role: tool` con `tool_call_id`. OmniProxy non esegue quel comando.

## Strumenti gestiti

Il profilo amministrativo può autorizzare Skill e server MCP. `app/capabilities.py` orchestra quelle chiamate. Non combinarle nella stessa richiesta con i tool eseguiti dal client.

## Attività agente

Modalità con lavori persistenti in uno spazio autorizzato, stati, eventi e
approvazione delle proposte. Il primo tipo `repo_patch` analizza e propone
modifiche senza eseguire comandi. Isolamento e permessi seguono il contratto
dei lavori, con tool disabilitati durante la generazione.

Il [contratto dei lavori gestiti](contratto-lavori.md) descrive gli endpoint
`/v1/jobs`, workspace, stati, approvazioni, idempotenza e confini dei file.
L'esecuzione di test tramite job usa ora un esecutore CLI autenticato con
profili autorizzati; vedi [esecutori](esecutori.md).

## Contratto HTTP

Il [contratto pubblico 1](../../spec/README.md), OpenAPI e suite indipendente
sono disponibili per la candidata `1.0.0-rc.1`. `/v1/capabilities` espone
protocolli, proprietà tool, configurazione e limiti della chiave, senza
contattare provider. Gli header `X-OmniProxy-Contract-Version` e
`X-OmniProxy-Version` distinguono contratto e prodotto. La compatibilità
è limitata ai sottoinsiemi descritti, non a tutte le API dei vendor.

`/v1/chat/completions` usa un alias modello controllato dal gateway. L'autenticazione usa una chiave applicativa. Lo streaming usa SSE e termina con `[DONE]`. Le richieste tool includono `tools`, `tool_choice` e `parallel_tool_calls`; le risposte devono avere `finish_reason: tool_calls` quando chiedono esecuzione al client.

`/v1/responses` espone un sottoinsieme testato del contratto Responses: testo,
immagini di ingresso dove il modello le supporta, function tool del client,
eventi SSE e continuità tramite `previous_response_id`. `store` è `false` per
impostazione predefinita; con `store: true` l'input e la risposta vengono salvati
in SQLite. `GET /v1/responses/{id}` recupera la risposta e `DELETE` la elimina.
Ogni risposta salvata è accessibile soltanto dalla chiave API che l'ha creata.
La cronologia ricostruita è limitata a 256 item e 2 MB. Per proseguire una
conversazione, il client può passare `previous_response_id` e soltanto il nuovo
input; il gateway concatena input e output precedenti. Il modello deve
coincidere. Tool paralleli e tipi di contenuto diversi da quelli documentati
sono rifiutati. Il gateway restituisce eventi `response.*` con `type` coerente
con il nome SSE. La conformità completa a Open Responses non è ancora
dichiarata: questo è un sottoinsieme versionabile da ampliare con test di
compatibilità esterni.

La [matrice delle capacità](capacita-adattatori.md) distingue percorsi
verificati, implementati e non supportati. Il gateway rifiuta contenuti audio,
`response_format` e output non testuale invece di ignorarli. Ollama accetta
immagini soltanto se `/api/show` dichiara `vision` per il modello selezionato.

Un upstream OpenAI compatibile può essere configurato dal server con
`UPSTREAM_BASE_URL` (URL base fino a `/v1`), `UPSTREAM_MODEL` e, se richiesta,
`UPSTREAM_API_KEY`. Espone l'alias `upstream` su `/v1/models` e usa lo stesso
contratto di autenticazione. È selezionabile nella dashboard dei profili
quando il catalogo remoto conferma il modello configurato. La migrazione
SQLite conserva chiavi, profili e collegamenti a Skill/MCP. Inoltra testo,
streaming e function tool del
client tramite Chat Completions; le immagini sono rifiutate. Il tool viene
eseguito soltanto dal client, che reinvia `role: tool`. L'URL e la chiave non
provengono dal client; la chiave non compare nelle risposte API.

`POST /v1/messages` accetta un sottoinsieme testuale di Anthropic Messages:
`model` è sempre un alias OmniProxy, `max_tokens` è obbligatorio, `messages`
contiene turni `user` e `assistant`, e `system` è testo opzionale. Supporta
risposta normale ed eventi SSE nativi. L'SDK Anthropic può autenticarsi con
`x-api-key` usando una chiave applicativa OmniProxy. Blocchi immagine, tool e
altri parametri non verificati vengono rifiutati. L'endpoint traduce verso lo
stesso routing del gateway; non implica un account Claude collegato.

`POST /v1beta/models/{alias}:generateContent` e
`POST /v1beta/models/{alias}:streamGenerateContent?alt=sse` offrono un
sottoinsieme testuale del protocollo Gemini; sono disponibili anche sotto
`/v1/models/`. L'SDK Google Gen AI può autenticarsi con `x-goog-api-key`
usando una chiave applicativa OmniProxy. `contents` accetta solo parti
`text`, con ruoli `user` e `model`; `systemInstruction` e alcuni parametri
`generationConfig` sono convertiti al contratto Chat. Tool e parti
multimodali sono rifiutati. L'alias nel percorso è scelto dal gateway e non
implica un account Gemini collegato.
