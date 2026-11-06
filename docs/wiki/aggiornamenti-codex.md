# Aggiornamenti Codex CLI

`codex-broker/version.txt` contiene la versione stabile desiderata. Il Dockerfile la legge durante la build. La variabile storica `CODEX_CLI_VERSION` in `.env` non controlla più le build Compose ordinarie; per una build manuale resta disponibile `docker compose build --build-arg CODEX_CLI_VERSION=<versione> codex-broker`.

Nel dialog Codex della dashboard, `GET /api/providers/codex/update` confronta la versione del broker con il tag `latest` del registro npm. Il pulsante chiama `POST /api/providers/codex/update`; il maintenance runner valida la versione, ricostruisce e ricrea solo `codex-broker`, verifica `codex --version` e aggiorna il pin dedicato. Lo stato del job è consultabile da `GET /api/providers/codex/update/jobs/{id}`.

Il controllo richiede accesso HTTPS a `registry.npmjs.org` dal gateway. Un aggiornamento può interrompere richieste Codex in corso durante la ricreazione del broker. Dopo l'aggiornamento verificare health, modello disponibile e un completamento reale prima di dichiarare compatibilità dei nuovi modelli.
