# Skills e server MCP

OmniProxy può associare capacità diverse a ogni API gestita senza cambiare
l'endpoint OpenAI-compatible usato dai client.

## Skill

Una Skill è un insieme di istruzioni Markdown riutilizzabili. Non contiene e
non esegue codice: viene aggiunta come istruzione di sistema solo alle API a
cui l'amministratore l'ha esplicitamente associata.

1. aprire **Skills & MCP** nella dashboard;
2. scegliere **Nuova Skill** e scrivere nome, descrizione e istruzioni;
3. creare o modificare un'API gestita;
4. selezionare le Skill che quell'API può usare e salvare.

Una Skill disabilitata non può essere associata a nuove API. L'eliminazione
rimuove automaticamente le associazioni esistenti.

## Server MCP

La prima versione supporta server MCP remoti tramite **Streamable HTTP** e
autenticazione senza credenziali oppure con token **Bearer**. Non esegue
comandi `stdio` arbitrari nel container del gateway.

1. avviare un server MCP raggiungibile dal container `gateway`;
2. aprire **Skills & MCP > Collega MCP**;
3. inserire l'endpoint completo, per esempio
   `http://home-assistant-mcp:3000/mcp` sulla stessa rete Docker oppure un
   endpoint pubblico `https://.../mcp`;
4. scegliere il metodo di autenticazione e salvare;
5. verificare lo stato **Connesso** e i tool scoperti;
6. creare o modificare un'API e autorizzare soltanto i singoli tool necessari.

Il modello può invocare esclusivamente la coppia server/tool autorizzata nel
profilo API. OmniProxy limita numero di chiamate, dimensione di schemi,
argomenti e risultati. L'audit salva server, tool, esito e latenza, ma non
argomenti né risultati.

## Sicurezza

- I token Bearer sono cifrati nel vault esistente e non vengono restituiti
  dalla dashboard o dalle API amministrative.
- Gli endpoint MCP pubblici devono usare HTTPS; HTTP è consentito soltanto per
  destinazioni private o loopback. Redirect e proxy d'ambiente sono disattivi.
- Descrizioni e risultati MCP sono trattati come dati non attendibili per
  ridurre il rischio di prompt injection.
- Una connessione riuscita non rende automaticamente disponibili tutti i
  tool: ogni tool richiede una allowlist per ciascuna API locale.
- Non collegare server MCP non fidati e assegna a ciascun server credenziali
  con privilegi minimi.

La versione corrente non implementa ancora OAuth MCP né l'approvazione umana
interattiva per singola chiamata. Per server esposti su Internet preferisci
HTTPS, token a breve durata e limitazioni di rete.

Riferimenti: [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk),
[client transports](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md)
e [specifica di autorizzazione MCP](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization).
