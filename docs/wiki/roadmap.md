# Roadmap

## Piano di prodotto — 5 novembre 2026

**Promessa di prodotto:** collegare account AI e modelli locali a qualsiasi
applicazione compatibile e offrire, sullo stesso gateway, la gestione di AI,
strumenti e lavori agente. Lo stesso core deve servire un singolo sviluppatore,
un team e un'organizzazione; le funzioni di amministrazione avanzata si
attivano solo quando servono. Le tappe indicano ordine e condizioni di uscita,
non date o compatibilità già raggiunte.

La promessa si sviluppa su due percorsi che condividono identità, profili,
catalogo modelli, permessi, metriche e dashboard:

| Percorso | Contratto pubblico | Primo valore per l'utente |
| --- | --- | --- |
| Accesso universale | API proxy per client, SDK e automazioni | Una chiave e un endpoint stabile per account cloud e modelli locali |
| Lavori gestiti | API per sessioni, strumenti e attività agente | Avviare, seguire, approvare e riprendere un lavoro in uno spazio autorizzato |

La gestione degli strumenti appartiene al client nella modalità proxy, al
server nella modalità MCP gestita e all'esecutore nella modalità agente. Il
contratto rende questa proprietà esplicita per evitare esecuzioni duplicate.

### Principi di prodotto

- Mantenere il percorso personale semplice: Docker Compose, dashboard locale,
  Ollama e account supportati, senza richiedere PostgreSQL o servizi esterni.
- Rendere esplicita la matrice delle capacità per modello e adattatore. Il
  catalogo non prova da solo accesso, streaming, tool o multimodalità.
- Separare proxy, tool MCP gestiti e lavori agente. Ogni esecuzione ha un solo
  proprietario degli strumenti e permessi visibili.
- Versionare il contratto pubblico e verificare ogni capacità con client reali.
- Tenere segreti, prompt e workspace entro confini documentati; le metriche
  predefinite non contengono contenuti delle richieste.

### Decisioni di ambito e ordine

1. **Un core, tre profili di distribuzione.** Personale: Compose e SQLite.
   Team: identità e limiti condivisi. Organizzazione: PostgreSQL, più repliche,
   audit e integrazione con i sistemi di osservabilità. Nessun profilo cambia
   la semantica delle API.
2. **Due contratti pubblici.** Il proxy risponde alle richieste dei client;
   il lavoro gestito ha identità, stato, eventi e permessi propri. I tool MCP
   gestiti appartengono al percorso server. Si riusano provider, catalogo,
   profili e autorizzazioni, non la stessa esecuzione di tool.
3. **Primo caso agente:** analizzare un repository autorizzato, proporre una
   patch e applicarla solo dopo approvazione e verifica dell'hash dei file.
   L'esecuzione di test è un'azione distinta con comando e output visibili.
4. **Ordine protocolli:** completare Chat Completions, poi Responses/Open
   Responses, quindi Anthropic Messages e Gemini `generateContent` in base a
   client reali. Ogni protocollo parte da un sottoinsieme documentato e
   verificato, senza promettere conversione totale.
5. **Ordine provider:** consolidare Ollama, Codex, Claude e Antigravity;
   aggiungere poi upstream OpenAI-compatible configurabili, inclusi server
   locali. Nuovi login nativi seguono domanda misurata e verifica dei termini
   dei provider.
6. **Ordine multimodale:** immagini in ingresso, documenti, immagini in
   uscita, audio; video solo dopo un caso d'uso e un contratto verificabili.
   La disponibilità è sempre per modello, provider e endpoint.

Ogni tappa comprende lavoro sul gateway e sul percorso agente. La tappa
successiva inizia solo dopo i test e la verifica dei criteri di uscita della
tappa precedente. Gli esperimenti possono procedere in parallelo senza essere
dichiarati supportati.

### Tappa 0 — Base affidabile

1. Completare i test end-to-end con Continue e provider autenticati, inclusi
   tool client, risposta `role: tool`, annullamento e streaming SSE.
2. Pubblicare una matrice verificata per adattatore: testo, immagini, tool,
   JSON strutturato, streaming, limiti di contesto e quote. Rifiutare in modo
   esplicito le combinazioni non supportate.
3. Aggiungere test di contratto riutilizzabili per dashboard, SDK OpenAI,
   automazioni e client di coding; misurare regressioni e latenza.
4. Definire il contratto minimo dei lavori gestiti: identità del lavoro,
   workspace, stato, eventi, annullamento, permessi e proprietario dei tool;
   vedi il [contratto v0](contratto-lavori.md).
5. Congelare una baseline di test, compatibilità, tempo aggiunto dal gateway,
   errori e consumo di memoria. Pubblicare i limiti per richiesta e per job.

**Uscita:** ogni capacità dichiarata nella documentazione passa un test reale
riproducibile; gli errori hanno codici e messaggi stabili. È disponibile una
baseline numerica su cui valutare le tappe successive. Il test reale con
Continue e account collegato è una condizione necessaria, non sostituibile
con il broker simulato.

### Tappa 1 — Interoperabilità

**Avanzamento nel checkout:** `/v1/responses` dispone ora di un sottoinsieme
implementato e testato per testo, function tool, SSE e continuità persistente
per chiave API. Un primo upstream OpenAI compatibile configurabile e il
contratto comune degli adattatori sono implementati, con test simulati e una
prova reale di testo/SSE tramite Ollama. I sottoinsiemi testuali Anthropic
Messages e Gemini generateContent sono implementati e provati con i rispettivi
SDK. L'upstream è integrato nei profili API con migrazione SQLite verificata.
Il primo lavoro `repo_patch` ha passato i test automatici e un ciclo reale
Codex con proposta, approvazione, applicazione e replay eventi. L'estensione
Continue 2.0.0 è verificata con tool nativi su VS Code Linux. Resta la
conformità esterna dei sottoinsiemi
dichiarati; non viene dichiarata la conformità completa a Open Responses.

1. Implementare `/v1/responses` con eventi di streaming, tool e continuità
   della conversazione definiti da un contratto versionato. Valutare la
   conformità a Open Responses senza dichiararla prima dei test.
2. Definire un'interfaccia per gli adattatori e il routing che permetta nuovi
   provider e endpoint OpenAI-compatible configurabili, anche locali.
3. Aggiungere esempi e test per i client che richiedono protocolli nativi
   Claude o Gemini; implementare gli adattatori richiesti dai casi d'uso.
4. Realizzare il primo lavoro agente: lettura di un repository autorizzato,
   proposta di patch, stato/eventi e annullamento. L'applicazione della patch
   richiede approvazione, controllo degli hash e un log verificabile; il job
   non esegue comandi arbitrari durante l'analisi.

**Uscita:** SDK OpenAI, un client di coding e un'automazione funzionano con i
percorsi proxy dichiarati; un lavoro agente produce una patch approvabile, è
osservabile e annullabile. Il test prova che un tool viene eseguito una sola
volta e che una patch obsoleta non viene applicata.

### Tappa 2 — Modelli, multimodalità e resilienza

**Avanzamento nel checkout:** allegati immagini/PDF/Office/testo, video con
fotogrammi campionati e trascrizione audio locale/upstream sono implementati.
Immagini e PDF scansionati sono provati con Codex; audio locale è provato
fino alla chat Ollama. Sono implementati limiti input/output/contesto per
chiave, compattazione e budget calendario con test reali locali. Sintesi
vocale nei sei formati e PNG neurale locale di prova sono verificati; i
backend e la qualità produttiva delle immagini richiedono configurazione.
Restano la verifica Windows e le esclusioni della matrice. Più deployment upstream, capacità
condivisa per account, cooldown 429 e fallback prima del contenuto sono
implementati, con prova reale tramite Ollama; i broker CLI mantengono ancora
una singola sessione ciascuno. L'esecutore CLI
autenticato dei test ha completato una prova reale Linux con Docker:
proposta Codex, test prima/dopo approvazione, isolamento e annullamento.
La compatibilità dell'esecutore Windows resta da verificare.

1. Completare immagini in ingresso su ogni broker che le supporta realmente;
   poi aggiungere output immagine e audio con endpoint e limiti distinti.
2. Consentire più account o deployment per provider con selezione, limiti di
   concorrenza, rilevamento quote, retry sicuri e fallback prima del primo
   output. Evitare replay automatici di tool o azioni con effetti esterni.
3. Migliorare Ollama con autodiscovery, metadati sulle capacità, streaming
   progressivo e limiti di contesto effettivi della configurazione locale.
4. Offrire esecutori Linux e Windows configurabili per i job, con comandi
   consentiti, confini del workspace e risultati visibili. Aggiungere export
   e import controllato delle configurazioni senza segreti in chiaro.

**Uscita:** ogni percorso multimodale ha test per formato, dimensione e
provider; i fallback non duplicano azioni e sono osservabili. Un job può
eseguire test consentiti nel workspace senza accesso a percorsi esterni.

### Tappa 3 — Team e organizzazioni

**Gate verificato il 5 novembre 2026:** identità/sessioni umane distinte dalle
chiavi applicative, tenant/progetti/ruoli, profili riutilizzabili, limiti e
revoca indipendenti, audit e diagnostica sono implementati. PostgreSQL 17.11
ha superato migrazione per hash delle righe, dump/restore e concorrenza da
otto processi, con ruolo runtime limitato. Worker e capacità upstream hanno
proprietà/lease condivisi; OTLP è verificato con collector reale e canarini
di privacy. Suite: 234 Python, 13 Node; container e servizio distribuito
verificati. I tre profili di installazione sono documentati. Il supporto
organizzazione verificato riguarda repliche Linux sullo stesso host con dati,
lock e workspace condivisi; non attesta multi-host o SLO di produzione.

1. Separare identità, chiave applicativa, profilo, progetto e tenant. Aggiungere
   ruoli, revoca, limiti per chiave, quote e budget.
2. Mantenere SQLite per l'istanza personale; offrire PostgreSQL e migrazioni
   verificabili per deployment condivisi e più repliche.
3. Esportare metriche e tracce con convenzioni OpenTelemetry GenAI,
   conservando un audit amministrativo distinto dai contenuti dei prompt.
4. Documentare installazione, backup, ripristino, upgrade e isolamento di rete
   per i tre profili di distribuzione.
5. Aggiungere diagnostica per provider e profilo, report di compatibilità e
   limiti di concorrenza; esercitare riavvio e migrazione con job in corso.

**Uscita:** un team può gestire accessi e limiti senza condividere la chiave
amministrativa; un upgrade conserva dati, permessi e stato dei job. La
configurazione personale resta avviabile senza PostgreSQL.

### Tappa 4 — Lavori agente maturi e documenti

**Gate verificato il 5 novembre 2026:** 248 Python, 14 Node; tre cicli reali
del gateway con Codex per persistenza, ripresa esplicita e singola scrittura;
citazioni di testo/PDF/OCR con hash e proprietà; SDK MCP 2.x e server sintetico
reali, senza duplicazione; migrazione e restore degli oggetti nuovi; immagine
non privilegiata/read-only verificata. Conservazione della cronologia opt-in,
profili job immutabili, hash di proposta legato a base/revisione e token worker
completano il percorso `repo_patch`. Non si promettono esecuzioni arbitrarie,
MCP non autorizzati, verità semantica dell'OCR o ripresa automatica di scritture
dall'esito incerto.

1. Rendere sessioni e lavori persistenti, interrompibili e riprendibili,
   con workspace isolati, gestione del contesto e approvazioni per le azioni
   sensibili.
2. Aggiungere upload documenti, PDF, OCR, immagini delle pagine e citazioni
   verificabili come capacità separate dal proxy di chat.
3. Estendere Skills e MCP con autorizzazioni per strumento, registri di
   esecuzione e test dei confini tra client e server.
4. Definire profili riutilizzabili per i job senza incorporare regole di una
   singola applicazione, come WikiNick, nel core OmniProxy.

**Uscita:** un lavoro riprende dopo riavvio senza perdere permessi o stato;
le citazioni rimandano a pagine e file verificabili.

### Tappa 5 — Contratto aperto e release 1.0

**Implementazione candidata verificata il 5 novembre 2026:** discovery per
chiave, header di versione, [specifica pubblica e OpenAPI](../../spec/README.md),
[suite indipendente dal core](../../conformance/README.md), esempi eseguibili,
versionamento/supporto, processo RFC e changelog sono realizzati. La suite
HTTP e i tre SDK OpenAI/Anthropic/Google hanno superato prove su socket reale
con Ollama, sia isolato sia sul gateway candidato distribuito. Continue nel
vero extension host e ripresa job Codex sono riprovati con la policy finale.
Gate: 250 Python, 14 Node; immagine read-only, backup e doppia migrazione
con hash delle 36 tabelle preesistenti superati. Report in `spec/reports/`.

**Pubblicazione candidata:** codice, specifica, suite ed evidenze minimizzate
sono inclusi nel candidato pubblico `1.0.0-rc.3`. Restano aperte
revisione/adozione indipendente, misure di utilizzo e promozione a stabile. Le tre integrazioni SDK sono esercitate dal progetto,
non equivalgono a due deployment indipendenti di terzi. La parte di codice
prevista è conclusa nel candidato; il criterio di adozione della roadmap
non viene dichiarato soddisfatto senza evidenza esterna.

1. Pubblicare specifica API, OpenAPI, suite di conformità, esempi e matrice
   di compatibilità indipendenti dall'implementazione.
2. Definire versionamento, deprecazioni e finestre di supporto prima della
   release 1.0; mantenere un processo pubblico per proposte e decisioni.
3. Integrare client e framework esterni, raccogliere casi d'uso riproducibili
   e misurare adozione, affidabilità e contributi esterni.

**Uscita:** terze parti possono implementare o verificare il contratto senza
leggere il codice OmniProxy; almeno due integrazioni esterne usano la suite di
conformità. La release 1.0 richiede compatibilità, migrazioni e supporto
documentati. L'adozione come standard dipende poi dall'uso esterno: non è
un'etichetta che il progetto può attribuirsi da solo.

### Pacchetti di lavoro immediati

| Ordine | Pacchetto | Dipende da | Evidenza di completamento |
| --- | --- | --- | --- |
| 0.1 | Baseline del checkout e test mirati | Nessuna | Risultati e limiti registrati senza modificare credenziali |
| 0.2 | Continue reale con Codex autenticato, normale e SSE | Account collegato | Ciclo `user → tool call → role: tool → risposta` ripetibile |
| 0.3 | Matrice capacità e rifiuto esplicito | 0.1–0.2 | Test per provider, modello e modalità; documentazione coerente |
| 0.4 | Contratto dei job e permessi del workspace | 0.3 | Schema, stati, eventi, annullamento e threat model rivisti |
| 1.1 | Responses con streaming e tool | 0.3 | Suite di contratto e client esterno funzionanti |
| 1.2 | Lavoro agente per patch approvata | 0.4 | Test su patch valida, obsoleta, annullata e tool non duplicati |

Le verifiche con account reali richiedono un account già collegato e un client
reale disponibile. Se mancano, si registrano come **non eseguite**, senza
considerare i test simulati equivalenti. Le tappe successive si dettagliano
in issue dopo l'uscita della tappa precedente.

### Criteri comuni per ogni capacità

- Contratto di input, output, errori, limiti e cancellazione pubblicato.
- Test unitari mirati, test di contratto e almeno un test end-to-end per il
  percorso dichiarato; esito registrato nella matrice delle capacità.
- Nessuna duplicazione di tool, patch o azioni nei retry e nei fallback.
- Segreti e contenuti esclusi dai log predefiniti; permessi e confini del
  workspace verificati con test negativi.
- Misure di latenza, errori, risorse e uso registrate rispetto alla baseline;
  ogni regressione significativa è spiegata prima del rilascio.
- Documentazione, migrazioni, esempi e procedura di rollback aggiornati nella
  stessa modifica del comportamento.

### Gate di rilascio e misure

| Profilo | Gate | Misure da pubblicare |
| --- | --- | --- |
| Personale | Installazione pulita, Ollama e un account cloud verificati, chiavi revocabili, backup/ripristino | Successo per percorso, latenza aggiunta, memoria, errori |
| Team | Ruoli e limiti per chiave, migrazioni ripetibili, concorrenza e riavvio con job attivi | Errori per provider, quote, job completati/annullati, recupero |
| Organizzazione | Repliche, audit, osservabilità, restore e upgrade provati, documentazione di rete | Disponibilità del gateway, p95/p99, capacità, tempi di ripristino |

I valori obiettivo si fissano dopo la baseline della tappa 0; non si inventano
SLO prima di misurare hardware e provider. Per ogni release si pubblicano
versione, piattaforme testate, compatibilità per provider e verifiche non
eseguite. La suite di conformità misura il contratto pubblico; le statistiche
di adozione includono integrazioni indipendenti e contributi esterni.

### Vincoli e rischi da sorvegliare

- Le quote e i termini degli account restano quelli dei provider. Nessuna
  funzione deve presentare un abbonamento come API ufficiale illimitata.
- L'evoluzione delle CLI e dei protocolli upstream richiede versioni fissate,
  test di regressione e un percorso di aggiornamento reversibile.
- L'aggiunta di audio, video e documenti amplia costi, superficie di attacco
  e requisiti di storage; ogni modalità ha limiti e retention espliciti.
- SQLite resta il percorso personale; PostgreSQL è una scelta di deployment
  condiviso, non una dipendenza obbligatoria del core.
- La roadmap non include prezzi o edizioni commerciali. Le capacità dei tre
  profili sono una scelta di deployment e autorizzazione.
