# Continue e strumenti client

Il gateway conserva i messaggi assistant con `tool_calls` e i risultati `role: tool`, `tool_call_id` e `name`. `CodexBrokerClient.complete_with_tools()` inoltra cronologia, definizioni e scelta degli strumenti al broker. Le risposte OpenAI normali e SSE includono `tool_calls` e `finish_reason: tool_calls`.

`codex-broker/client-tools.mjs` avvia un app-server effimero per richiesta, registra i tool tramite `dynamicTools`, disabilita gli strumenti server principali e restituisce al client un tool per volta. Il client Continue possiede l'esecuzione e la conferma. Il processo viene chiuso alla fine del turno o alla disconnessione.

Il broker rileva anche istruzioni testuali `tool_definition` e `TOOL_NAME:` nei messaggi system/developer. Il protocollo testuale Continue 2.0.0 va ancora verificato con Continue reale; la sola rilevazione non prova che il blocco `tool` prodotto sia corretto.

I test locali sono `tests/test_continue_tools.py` e `codex-broker/client-tools.test.mjs`. La versione Codex nell'immagine costruita è riportata in [Aggiornamenti Codex](aggiornamenti-codex.md). Non inferire l'assenza di strumenti solo da `tools=0`.

Sul server Linux sono installati VS Code 1.109.3, Continue 2.0.0 e Continue
CLI 1.5.47. Il comando `~/.local/bin/cn` usa un runtime Node 22
isolato sotto `.local/share/omniproxy-tools/continue`, senza sostituire Node
di sistema. L'installazione sul server non attesta l'installazione del VS
Code Windows né l'esecuzione reale dell'estensione IDE. Il progetto resta
sul server e viene aperto dal client tramite Remote SSH.

La verifica successiva `scripts.verify_vscode`, con display virtuale e dati
utente temporanei, ha ora eseguito il vero extension host Continue 2.0.0:
chat SSE, function call nativa, lettura file tramite il tool del client e
risposta finale con il marker atteso. Il test hook usa un mirror temporaneo
del pacchetto per i percorsi dei worker; il pacchetto installato non viene
modificato. La prova non certifica il protocollo testuale della GUI né il
client Windows.
