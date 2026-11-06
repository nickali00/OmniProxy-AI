# Architettura

```text
Client → gateway FastAPI → route/profilo → provider
                                    ├─ Ollama
                                    ├─ Codex broker
                                    ├─ Claude broker
                                    └─ Antigravity broker
              └─ SQLite: configurazione, chiavi e metriche
Dashboard → gateway → maintenance runner → Docker Compose
```

`app/main.py` gestisce API e dashboard. `app/schemas.py` valida richieste e configurazioni; `app/provider_broker.py` parla con i sidecar. `app/capabilities.py` orchestra Skill e MCP autorizzati dal profilo. `provider-common/runtime.mjs` offre parsing comune ai broker. `codex-broker/server.mjs` usa Codex CLI e app-server; `codex-broker/client-tools.mjs` gestisce i tool posseduti dal client.

Le credenziali dei provider restano nei rispettivi volumi. Il gateway non deve registrare prompt o segreti nelle metriche. Il maintenance runner ha accesso al socket Docker e solo a comandi consentiti; il suo unico mount scrivibile per gli aggiornamenti Codex è `codex-broker/version.txt`.

Separare sempre chiave applicativa, profilo, spazio di lavoro, sessione e singolo lavoro. Il core non deve assumere che una chiave identifichi una memoria condivisa. La persistenza di lavori e isolamento per spazio/sessione sono obiettivi di evoluzione, non capacità già verificate.
