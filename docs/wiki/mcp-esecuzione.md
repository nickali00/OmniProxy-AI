# MCP gestito: proprietà e verifiche

Il gateway espone al modello soltanto i tool selezionati nel profilo e li
chiama tramite SDK MCP, con timeout e limiti di risultato. Le risposte sono
dati non fidati marcati `mcp_tool_result`, separati dalla cronologia nativa
dei function tool client. Il broker non deve interpretarli come richieste
di esecuzione del client. Le modalità client e MCP gestito nella stessa
richiesta sono rifiutate prima di prenotare budget.

Dentro una richiesta, la stessa chiamata con gli stessi argomenti JSON
canonici usa il risultato già ottenuto. Anche un errore dall'esito incerto
non viene ritentato automaticamente. L'audit conta soltanto esecuzioni reali
e conserva tool, esito, durata e codice errore, senza argomenti o risultati.
Non si promette deduplicazione semantica di argomenti diversi o di nuove
richieste inviate dal client.

Il broker Codex usa una policy comune per testo e app-server: disabilita shell,
app, browser, computer, generazione immagini, lettura immagini locali,
subagenti, ricerca web e registri MCP interni. L'immagine inviata dall'utente
resta un input distinto dal tool di lettura del filesystem. Le opzioni sono
per singola sessione e non cambiano la configurazione dell'account. Vedi la
[CLI ufficiale](https://learn.chatgpt.com/docs/cli/reference) e la
[ricerca web](https://learn.chatgpt.com/docs/web-search?surface=cli).

Il percorso Codex con function tool client rifiuta esplicitamente immagini
nella stessa richiesta (`client_tools_images_not_supported`); non le elimina
silenziosamente. Testo con tool client e immagini senza tool sono percorsi
distinti, già verificati. La combinazione rimane fuori dal supporto dichiarato.

`scripts.verify_mcp` avvia temporaneamente un server MCP 2.x solo loopback,
con un tool sintetico in sola lettura. Verifica discovery e RPC reali,
orchestrazione con Codex autenticato, una sola esecuzione, conflitto con
tool client e budget regolato. I test automatici verificano anche una
richiesta duplicata del modello senza ripetere la RPC. Non è una prova
contro i sistemi personali dell'utente o contro tutti i server MCP esistenti.
