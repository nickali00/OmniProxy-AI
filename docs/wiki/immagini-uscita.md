# Immagini in uscita

`POST /v1/images/generations` richiede una chiave applicativa, `model: image`,
`prompt`, `size`, `n` (1–4), `response_format: b64_json`. Restituisce PNG in
base64, senza conservare immagini o recuperare URL restituiti da upstream.
Il backend deve essere configurato esplicitamente: in assenza risponde 503
`image_provider_not_configured`.

Configurare `IMAGE_BACKEND_URL` (base `/v1`), `IMAGE_MODEL` e, se richiesta,
`IMAGE_API_KEY` lato server. Le credenziali non provengono dalla richiesta.
In alternativa abilitare `ENABLE_LOCAL_IMAGES=true` durante la build e
montare un modello Diffusers in sola lettura nel percorso `IMAGE_LOCAL_MODEL`.
Il worker locale usa CPU, safetensors e file locali; non esegue codice remoto
e non scarica pesi durante le richieste. L'installazione separata dei pesi è
disponibile con `python -m scripts.install_image_model`, repository e revisione
immutabile espliciti. Valutare licenza, RAM e qualità del modello scelto.

Sono accettati lati 256/512/768/1024/1536/2048, entro `IMAGE_MAX_PIXELS`
(default 1.572.864); ogni PNG è limitato a 6 MiB. Le risposte upstream vengono
decodificate e normalizzate in un sottoprocesso con limiti di risorse,
verificando numero e dimensioni. Il worker locale ha un solo slot e timeout
configurabile. La disconnessione interrompe HTTP e sottoprocesso.

I limiti input/output/contesto della chiave si applicano anche qui. Le unità
output sono una **stima amministrativa**, default 4096 per megapixel, non la
tokenizzazione nativa del modello. Il budget condiviso deve coprire
l'immagine intera: altrimenti 429 con reset e Retry-After, senza generazione.
Dopo invio al backend, errori e annullamenti addebitano conservativamente la
stima riservata: l'upstream può aver già consumato risorse. Gli errori locali
di preflight non addebitano output.

Verifiche: test di normalizzazione, dimensioni, credenziali lato server,
assenza di fetch URL, quote e rilascio prenotazioni. `scripts.verify_images`
ha prodotto un PNG con runtime neurale reale e il modello pubblico
`hf-internal-testing/tiny-sdxl-pipe` fissato alla revisione
`20594cbc343cfcfe447af5c87cdaf6c436b453f2`. È un fixture di integrazione:
questa prova **non attesta qualità fotografica o qualità da produzione**.
Il deployment corrente non ha un modello immagini di produzione configurato.
