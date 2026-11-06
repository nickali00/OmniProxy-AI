# Audio in uscita

`POST /v1/audio/speech` produce audio locale con eSpeak NG e FFmpeg.
Richiede una normale chiave API; input, output e budget seguono la politica
della chiave. Il modello pubblico è `speech-local`: è una sintesi leggera
offline, non una voce neurale. Le risposte Chat Completions/Responses restano
testuali; l'audio è un endpoint separato.

```json
{"model":"speech-local","input":"Il colore è blu.","voice":"it",
 "response_format":"wav","speed":1}
```

Voci: `it`, `en-us`, `en-gb`, `es`, `fr`, `de`. Formati: `mp3` (predefinito),
`wav`, `flac`, `opus`, `aac`, `pcm`. PCM è mono, 24 kHz, signed 16-bit little
endian; gli header espongono sample rate e canali. Velocità da 0,5 a 2,
testo massimo 4096 caratteri. Opzioni non implementate vengono rifiutate.

Il consumo dell'output è una **stima per durata logica del parlato**:
`SPEECH_OUTPUT_TOKENS_PER_SECOND`, predefinito 50. I limiti riducono la durata
restituita, mantenendo valido il formato. Gli header
`X-OmniProxy-Output-Truncated`, `X-OmniProxy-Input-Tokens`,
`X-OmniProxy-Output-Tokens`, `X-OmniProxy-Audio-Duration` e quelli del budget
espongono l'esito. La durata logica esclude l'eventuale padding del codec;
non sono token di fatturazione di un servizio esterno.

Limiti tecnici: 120 secondi per risposta, timeout 45 secondi, due processi
attivi e 10 MiB di output. Il subprocess riceve un ambiente senza chiavi dei
provider e limiti CPU, memoria e file. PulseAudio è disattivato nel processo
di sintesi su file; non si apre l'audio del desktop. Timeout e disconnessione
terminano il gruppo dei processi e rilasciano la prenotazione del budget.

I test usano eSpeak/FFmpeg reali per tutti i sei formati, taglio valido del
WAV, budget ed errori. `scripts.verify_speech --audio-model-path PATH` ha
superato testo → voce locale → trascrizione tiny su CPU, riconoscimento del
contenuto e conteggio di entrambe le operazioni sullo stesso budget.
Il runtime è incluso nella nuova immagine Docker, da costruire e verificare
prima di dichiarare il percorso disponibile sul servizio distribuito.
