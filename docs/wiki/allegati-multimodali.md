# Allegati multimodali

Il gateway accetta upload con `POST /v1/files` (multipart `file`, purpose
`user_data` o `assistants`). Elenco paginato, metadati, download e cancellazione
sono sotto `/v1/files`. Ogni file appartiene alla chiave autenticata: altre
chiavi ricevono 404, anche se conoscono l'ID. Il download usa sempre
`Content-Disposition: attachment` e `nosniff`.

| Formato | Elaborazione attuale |
| --- | --- |
| PNG, JPEG, WebP, GIF, BMP, TIFF | Validazione, normalizzazione JPEG e ingresso vision; primo frame per immagini animate |
| PDF | Testo con marcatori pagina; pagine prive di testo rese come immagini e OCR quando disponibile |
| DOCX | Testo con marcatori di paragrafo |
| XLSX | Fogli e celle, formule e valori memorizzati; nessun ricalcolo |
| PPTX | Testo delle slide con marcatori |
| ODT | Testo dei paragrafi |
| Testo, CSV, JSON, Markdown, HTML, codice | Contenuto testuale; nessuna esecuzione o rendering HTML |
| MP4, MOV, M4V, WebM, MKV | Quattro fotogrammi campionati con timestamp; modello vision richiesto |
| WAV, MP3, M4A, FLAC, OGG, Opus, AAC | Normalizzazione PCM e trascrizione locale o backend compatibile configurato |

Le immagini e le pagine PDF visive richiedono un modello vision; il gateway
non attribuisce questa capacità a un modello testuale. Office è estrazione
testuale: immagini incorporate e layout non vengono interpretati. Il video
è un campionamento esplicito: non equivale ad analizzare ogni fotogramma e
non include automaticamente il parlato. Con `mode: "text"` un video viene
trascritto, senza analisi visiva, se il backend audio è configurato.

## Uso nelle conversazioni

Chat Completions: parte `{"type":"file","file":{"file_id":"file-..."}}`
in un messaggio `user`. Responses: parte
`{"type":"input_file","file_id":"file-..."}` oppure `input_image` con
`file_id` per una singola immagine. Il file viene risolto prima del conteggio
token: testo estratto, metadati e marcatori entrano nel limite di ingresso,
nel contesto e nel budget della chiave. Le immagini usano la stima dichiarata
del gateway, non la tokenizzazione autorevole del modello.

La proprietà opzionale `mode` (`auto`, `text`, `vision`) sceglie l'estrazione:
`vision` include le pagine PDF come immagini, `text` usa il testo estraibile
(o OCR delle immagini). `auto` usa testo PDF quando presente e aggiunge
pagine visive quando il testo manca. I messaggi includono ID del file e
marcatori pagina/paragrafo/cella per identificare le fonti; non sono una
certificazione automatica della correttezza delle citazioni del modello.

## Limiti e conservazione

Predefiniti configurabili sul server: 20 MiB per file, 100 MiB di spazio per
chiave, sette giorni di conservazione, massimo 1.000 file per chiave e otto
allegati per richiesta. Spazio e quote comprendono anche i risultati di
estrazione. I file scaduti non sono accessibili e vengono rimossi alla
successiva operazione di upload; SQLite riutilizza lo spazio liberato.

L'estrazione usa subprocess con timeout e limiti di memoria, CPU e output.
Sono ammessi al massimo 100 pagine PDF testuali, otto pagine visive, 250.000
caratteri estratti e 10.000 celle XLSX. Archivi Office vengono letti senza
estrarre percorsi sul filesystem; entità XML esterne, archivi eccessivi,
file cifrati e formati non supportati vengono rifiutati. Non si eseguono
macro, formule o programmi allegati. Il processo di estrazione riceve un
ambiente senza chiavi dei provider.

`tests/test_files.py` copre parser reali, isolamento, file_id, quote e input
ostili. `scripts/verify_attachments.py` ha completato upload, analisi reale
con Codex di un'immagine e di una pagina PDF scansionata e cancellazione,
usando database e chiavi temporanei. Anche il video campionato è stato
interpretato da Codex nella stessa prova. L'immagine gateway è stata verificata
con parser reali e distribuita; il backend audio è un'opzione da configurare.

## Audio

`POST /v1/audio/transcriptions` accetta multipart `file`, modello pubblico
`transcription`, `language` opzionale e `response_format` `json` o `text`.
Il JSON dichiara stime token e troncamento; anche il formato testo espone
`X-OmniProxy-Output-Truncated` e gli header di budget. Gli stessi formati audio
possono essere caricati in `/v1/files` e usati via `file_id` nella chat.

Configurare `AUDIO_BACKEND_URL`, `AUDIO_MODEL`, eventuale `AUDIO_API_KEY`,
oppure un modello faster-whisper già presente in `AUDIO_LOCAL_MODEL`.
Il runtime locale è opzionale (`requirements-media.txt`, build Docker con
`ENABLE_LOCAL_AUDIO=true`); il modello deve essere montato in sola lettura.
`scripts.install_audio_model` prepara un modello locale senza scaricarlo
implicitamente durante una richiesta. Nessun backend: HTTP 503 esplicito.

Predefiniti: 300 secondi di audio, 120 secondi di video, quattro fotogrammi,
timeout di trascrizione 120 secondi e un trascrittore locale attivo per volta.
I token audio di ingresso sono una **stima configurabile**
(`AUDIO_INPUT_TOKENS_PER_SECOND=50`), distinta dai token del testo restituito.
Trascrizione e successiva inferenza della chat consumano entrambe il budget
della chiave. L'output audio generato e l'audio in tempo reale restano esclusi.

`scripts.verify_audio --model-path PATH` ha superato un test CPU reale con
faster-whisper tiny: audio sintetico, trascrizione diretta, allegato audio,
comprensione della trascrizione con Ollama `qwen3.5:2b` e contabilizzazione.
I test video usano FFmpeg reale e verificano durata, fotogrammi e rifiuto
di playlist esterne; non attestano comprensione audiovisiva integrata.
