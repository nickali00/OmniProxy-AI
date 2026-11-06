# Telemetria e audit

La telemetria è disattivata per default. `OTEL_ENABLED=true`,
`OTEL_ENDPOINT=http://collector:4318` e `OTEL_SAMPLE_RATE` abilitano OTLP HTTP
per trace e metriche verso il collector gestito dall'operatore. Nessun nuovo
listener viene aperto dal gateway. Gli exporter hanno timeout e code limitati.

Le chiamate chat, tool e sintesi di contesto producono span client con
`gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model` e
conteggi input/output. HTTP conserva il template della route, metodo e stato;
lo span termina dopo il corpo SSE. È accettato contesto W3C, senza baggage.
I nomi di modello arrivano dal routing del server; modelli non risolti sono
etichettati `unresolved`, senza esportare la stringa fornita dal client.

La durata inferenza usa `gen_ai.client.operation.duration` in secondi. I
conteggi amministrativi sono esposti separatamente come
`omniproxy.estimated_token.usage`: sono stime gateway e non token nativi o
credito del provider. `omniproxy.requests` conta le operazioni contabilizzate.
Vedi le [convenzioni GenAI](https://github.com/open-telemetry/semantic-conventions-genai/tree/main/docs/gen-ai).

Sono esclusi prompt, risultati, allegati, argomenti tool, chiavi, intestazioni,
query string, URL upstream e messaggi delle eccezioni. Gli errori conservano
soltanto il codice stabile; non sono registrati stack trace con contenuti.
Non è attivata strumentazione automatica dei client HTTP o delle query SQL.
Il test usa un collector HTTP locale reale e decodifica i messaggi Protobuf,
verificando l'assenza di canarini in prompt, output, header, query ed errori.

L'audit amministrativo è un registro DB distinto: identità, tenant, operazione,
ID oggetto, esito e data. Le mutazioni team riuscite lo scrivono nella stessa
transazione; azioni globali legacy e richieste autorizzate rifiutate hanno
copertura middleware. Non è un registro crittograficamente immutabile contro
l'amministratore del database. `/admin/v1/tenants/{id}/audit` è per admin del
tenant; `/admin/v1/audit` e `/admin/v1/diagnostics` sono riservati all'operatore.
La diagnostica progetto aggrega uso/errori/job delle sole chiavi del progetto.
