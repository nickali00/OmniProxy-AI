# Estrazioni e citazioni verificabili

`GET /v1/files/{id}/extraction?mode=auto|text|vision` restituisce unità testuali,
hash SHA-256 dei byte originali e di ogni unità, avvertenze e scadenza. PDF:
un'unità `page-N` per pagina. Testo, Office, OCR e trascrizioni: unità
`document-1`; i riferimenti a paragrafi, slide e celle restano nel testo
estratto. L'offset è in **punti di codice Unicode**, non byte o unità UTF-16.
Immagini senza OCR esplicito possono non avere testo citabile.

`POST /v1/citations/verify` verifica il riferimento senza conservarlo;
`POST /v1/citations` lo conserva e restituisce un ID `cite_...`.
Campi richiesti:

```json
{
  "file_id": "file_ID_RESTITUITO_DALL_UPLOAD",
  "unit_id": "page-2",
  "mode": "auto",
  "source_sha256": "HASH_DEI_BYTE_ORIGINALI",
  "text_sha256": "HASH_DELL_UNITA_ESTRATTA",
  "start": 0,
  "end": 4,
  "quote": "BLUE"
}
```

Hash e ID nell'esempio sono segnaposto. Il server richiede hash esadecimali
di 64 caratteri e ID nel formato restituito dall'upload. Verifica proprietario,
scadenza, hash dei byte effettivi, unità/pagina, hash del testo e corrispondenza
esatta dell'intervallo. Una frase presente su un'altra pagina non convalida
il riferimento. Errori: `citation_unit_not_found`, `citation_source_changed`,
`citation_quote_mismatch`; gli errori non riflettono la citazione fornita.

La creazione dello stesso riferimento è idempotente. Il registro conserva
locator e hash, senza duplicare l'estratto. `GET /v1/citations/{id}` ricostruisce
e verifica nuovamente la citazione; `DELETE` elimina il riferimento. Sono
ammessi al massimo 1000 riferimenti per chiave. Scadenza o eliminazione del
file rendono il riferimento indisponibile; l'eliminazione del file rimuove
anche i relativi locator. L'URL dell'originale resta autenticato, senza link
pubblici contenenti il token.

Questa è verifica della provenienza e dell'estrazione, non della verità della
frase o dell'accuratezza dell'OCR. Layout Office, formule ricalcolate e
interpretazione visiva restano capacità distinte. Le risposte di chat già
memorizzate hanno un ciclo di conservazione separato dagli upload.

Prove reali locali: testo Unicode, PDF multipagina, OCR di un'immagine,
proprietà tra chiavi, frasi/hash/intervalli falsi, cache o byte alterati,
scadenza ed eliminazione. I nuovi locator sono inclusi nella prova di
migrazione e dump/restore PostgreSQL.
