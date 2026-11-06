# Stato corrente

## Candidato pubblico 1.0.0-rc.4 — 6 novembre 2026

Il candidato rc.4 limita anche i thread di FFmpeg nel worker vocale, senza
allentare memoria o timeout. La CI installa i tool multimediali su Ubuntu
24.04; la sintesi nativa su 26.04 non è qualificata. Nessuna migrazione DB
o modifica delle credenziali è richiesta.

Il candidato include proxy OpenAI-compatible, sottoinsiemi Responses,
Anthropic e Gemini, modelli locali e account cloud supportati, tool client,
MCP gestito e job con proposta/approvazione della patch. Include limiti
input/output/contesto, compattazione e budget per chiave, allegati e media,
gruppi richiudibili, team, PostgreSQL, osservabilità, migrazioni e backup.
Le capacità effettive restano specifiche per adattatore e modello:
[matrice](capacita-adattatori.md), [compatibilità](../../spec/compatibility.md).

Il profilo HTTPS usa Caddy su socket Unix privato, dashboard autenticata,
CA esplicita sui client, protezione Host/Origin e controllo maintenance
autenticato. HTTP API riceve 426; chiavi, stati, limiti e alias preesistenti
sono preservati. Il login ha CSS esterno dedicato; l'indicatore gateway
mostra l'URL reale e conserva eventuali porte personalizzate.

La CLI locale permette estensioni esplicite di 1–5 anni per sessioni
operatore attive; le emissioni ordinarie restano 1–168 ore. I promemoria
privati Telegram controllano certificati e metadati di scadenza dashboard
senza ricevere il token, con deduplicazione persistente degli invii ambigui.
Vedere [sicurezza e HTTPS](sicurezza-https.md) per attivazione e limiti.

## Evidenze e limiti

- Gate Python completo delle funzionalità di sicurezza/sessione: 283 test.
- Gruppi/dashboard e broker: test Node e renderer su DOM minimo.
- HTTPS reale: TLS 1.2/1.3, rifiuto 1.0/1.1, CA verificata, SDK e SSE.
- Immagine non privilegiata/read-only, backup e doppia migrazione con confronto
  per hash, integrità e foreign key; nessuna migrazione distruttiva dei dati.
- Client: test Python/Rust/web, inferenze visive su nove profili attivi,
  motore n8n reale e test negativo senza CA. Le credenziali già disattivate
  sono rimaste disattivate. Nessun comando fisico ai robot.
- Continue nel vero extension host Linux, tool nativi e lettura client;
  non certifica il protocollo testuale GUI o il client Windows.

I [report](../../spec/reports/) pubblici contengono evidenze minimizzate.
Percorsi personali, topologia, identificativi operativi, impronte della CA,
scadenze effettive e backup privati non fanno parte del repository pubblico.
Il runbook completo dell'installazione è conservato separatamente.

Restano avvisi di sistema/vendor e confini fidati documentati in
[sicurezza](sicurezza-https.md). L'account Claude nativo non è disponibile
per una prova completa; Claude tramite Antigravity è stato verificato.
Un modello Gemini tramite Antigravity ha restituito errore di esecuzione:
un account collegato non prova tutti i modelli. Nessuna prova grafica del
browser o installazione CA Windows è stata eseguita in questa sessione.

## Pubblicazione e adozione

[Gate rc.4](../../spec/reports/publication-rc4-2026-11-06.json), dopo il
[gate iniziale di pubblicazione](../../spec/reports/publication-2026-11-06.json):
283 Python anche su copia pubblica senza file privati, 16 Node, OpenAPI e
Compose; scanner di segreti e confronto con credenziali locali senza riscontri.

Questa pubblicazione rende disponibili codice, specifica, suite di conformità,
esempi e matrice. La versione resta candidata, non stabile. Revisione e
adozione indipendenti e misure d'uso richiedono evidenze esterne; non si
dichiara che OmniProxy sia già uno standard adottato.
[Roadmap](roadmap.md), [verifiche e rilascio](verifiche-rilascio.md).

## Dipendenze opzionali immagini

GitHub segnala cinque avvisi (quattro high, uno medium) su
`transformers==4.57.6` in `requirements-images.txt`: non sono risolti
dall'audit dei soli requisiti core. La dipendenza non è presente nel gateway
attivo verificato ed è esclusa dalla build predefinita. Il percorso locale
usa file preinstallati e safetensors; ciò non certifica l'irraggiungibilità
di tutti gli avvisi. Un aggiornamento richiede verifiche dedicate del runtime
immagini. Gli avvisi sono visibili nel pannello Dependabot del repository.
