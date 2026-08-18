# Home Assistant connector

OmniProxy AI includes a custom Home Assistant integration that adds a
conversation agent to Assist. Home Assistant connects to one managed
OmniProxy API, while the provider, model and reasoning profile remain enforced
by the gateway.

The connector supports text/voice conversation, questions about current Home
Assistant states, safe local home-control intents and use from
`conversation.process` automations. Device commands are executed by Home
Assistant's native Assist intent engine before conversational requests are
sent to OmniProxy. The model never receives permission to call arbitrary Home
Assistant services.

## 1. Create the dedicated API

1. Open the OmniProxy dashboard.
2. Connect the desired provider or enable Ollama.
3. Create a managed API named **Home Assistant**.
4. Choose provider, model and reasoning level.
5. Copy the `sk-local-...` key when it is shown. It cannot be recovered later.

## 2. Install with HACS

1. Open **HACS > Integrations** in Home Assistant.
2. Open the three-dot menu and choose **Custom repositories**.
3. Add `https://github.com/nickali00/OmniProxy-AI` with category
   **Integration**.
4. Install **OmniProxy AI** and restart Home Assistant.

Manual installation is also possible: copy
`custom_components/omniproxy_ai` from this repository into
`<home-assistant-config>/custom_components/omniproxy_ai`, then restart Home
Assistant.

## 3. Connect

In Home Assistant, open **Settings > Devices & services > Add integration**,
search for **OmniProxy AI**, and enter:

- **Base URL:** one of the URLs below;
- **Local API key:** the dedicated `sk-local-...` key.

The connector validates the key through `/v1/models` and automatically uses
the model slug bound to it. No provider credential is stored in Home
Assistant.

### URL and port

The default host port is `8000`. If `GATEWAY_PORT` is customized in `.env`,
replace `8000` in host/LAN URLs with that value. The container-to-container
port always remains `8000`.

Choose the URL that matches the deployment:

| Home Assistant location | Base URL |
| --- | --- |
| Same Ubuntu host, HA Core or HA Container using host networking | `http://127.0.0.1:8000/v1` |
| Container attached to `omni-proxy-ai-network` | `http://gateway:8000/v1` |
| Another trusted LAN machine | `http://<OMNIPROXY_HOST_IP>:8000/v1` |

When Home Assistant has its own Compose file, attach its service to the
existing network:

```yaml
services:
  homeassistant:
    networks:
      - omniproxy

networks:
  omniproxy:
    external: true
    name: omni-proxy-ai-network
```

The URL in this case is `http://gateway:8000/v1`; the host port is not used.

For a separate LAN host, OmniProxy must listen on the LAN interface:

```dotenv
GATEWAY_BIND_ADDRESS=0.0.0.0
GATEWAY_PORT=8000
DASHBOARD_ALLOWED_HOSTS=127.0.0.1,localhost,gateway,<OMNIPROXY_HOST_IP>
```

Then recreate the gateway:

```bash
docker compose up -d --build --force-recreate gateway
```

Binding to `0.0.0.0` also makes the administrative dashboard reachable on the
LAN. Restrict TCP port `8000` with the host firewall to the Home Assistant IP,
or prefer the shared Docker network. Never forward this port from the router
and never expose it directly to the Internet.

## 4. Select it in Assist

Open **Settings > Voice assistants**, edit or create an Assist pipeline, and
select the new **OmniProxy AI** conversation agent. The integration options
allow you to change instructions, maximum response tokens, temperature and
conversation history length without changing the managed gateway API.

The same agent can be called by an automation through Home Assistant's
`conversation.process` action.

### Safe device control

Commands recognized by Home Assistant's built-in Assist grammar are handled
locally with the original user context. Only entities exposed to Assist can be
targeted. For example:

- `Accendi il condizionatore di Nicola.`
- `Spegni le luci della cucina.`
- `Imposta il condizionatore a 23 gradi.`

Give each target a clear entity name or alias. If the local intent engine does
not recognize a command, it falls back to OmniProxy as a normal conversation;
the LLM is not allowed to invent or execute a service call.

For Italian climate commands, the connector safely bridges explicit phrases
such as `accendi il condizionatore di Nicola` to Home Assistant's native intent
handler because the standard Italian `HassTurnOn` sentence set does not include
the `climate` domain. Add `condizionatore di Nicola` as an alias on the actual
`climate.*` entity, not on its separate temperature sensor. Pronouns such as
`accendilo` are intentionally not resolved to a device. The connector accepts
the longer form `condizionatore della stanza di Nicola` and normalizes a small
set of unambiguous speech-to-text mistakes, while final target matching remains
inside Home Assistant.

## 5. Let the agent read home states

Open **Settings > Voice assistants > Expose** and expose only the entities the
agent is allowed to read. Battery sensors are not generally exposed by
default, so select each desired battery entity explicitly.

For every question, the connector searches the exposed catalog locally using
the entity name, entity ID, aliases, area, device class and multilingual terms.
It sends only the matching states to OmniProxy. The default limit is 40
entities per request and can be raised to 100 from the integration options for
broad catalog questions; specific questions normally select far fewer states.
Arbitrary entity attributes are discarded. This behavior can be disabled from
the OmniProxy AI integration options with **Read relevant entities exposed to
Assist**.

Examples:

- `Quali sensori riesci a vedere?` (lists only exposed sensor entities)
- `Quali batterie sono sotto il 20%?`
- `Che temperatura c'è in cucina?`
- `La finestra della camera è aperta?`

Use clear entity names and aliases for the best local match. If no exposed
entity matches the question, the agent will report that the information is not
available instead of inventing a state.

---

## Guida rapida in italiano

1. In OmniProxy crea un'API gestita chiamata **Home Assistant** e copia la
   chiave locale.
2. Installa questo repository come integrazione personalizzata da HACS.
3. Riavvia Home Assistant e aggiungi l'integrazione **OmniProxy AI**.
4. Usa `http://127.0.0.1:8000/v1` se Home Assistant gira sullo stesso host con
   rete host. Se hai cambiato `GATEWAY_PORT`, sostituisci `8000` con quel valore.
5. Incolla la chiave: modello e provider vengono rilevati automaticamente.
6. Seleziona l'agente nella pipeline Assist.
7. Apri **Impostazioni > Assistenti vocali > Esponi** e abilita per Assist i
   soli sensori che vuoi rendere leggibili, comprese le batterie desiderate.
8. Per comandare un dispositivo, esponilo ad Assist e assegnagli un nome o
   alias chiaro. L'esecuzione avviene localmente in Home Assistant, non nel
   provider AI.

Se Home Assistant è un container sulla rete Docker di OmniProxy, usa invece
`http://gateway:8000/v1`.
