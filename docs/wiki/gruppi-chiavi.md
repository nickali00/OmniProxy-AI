# Gruppi di chiavi nella dashboard

Nella vista API il modulo **Nuovo gruppo** crea un contenitore, per esempio
`Studio` o `Robot`. Su ogni scheda chiave il menu **Gruppo** consente di
assegnarla a un contenitore o riportarla a **Senza gruppo**. Ogni chiave
appartiene a un solo gruppo. Non sono previste cartelle annidate.

Premere il nome/la freccia del gruppo per richiuderlo o riaprirlo. Il numero
delle chiavi resta visibile anche a gruppo chiuso. Nome, appartenenze e stato
aperto/chiuso sono salvati nel database e condivisi dalla dashboard dopo
refresh, riavvio o accesso da un altro browser. Si possono rinominare ed
eliminare i gruppi; eliminare un gruppo sposta le sue chiavi in Senza gruppo.

I gruppi organizzano i profili mostrati nella dashboard: non cambiano chiavi,
alias modello, stato attivo/in pausa, quote, permessi o strumenti. Non sono
tenant/progetti né un nuovo livello di autorizzazione. In modalità team la
dashboard globale e queste operazioni restano riservate all'operatore.

API della dashboard, escluse dall'OpenAPI pubblico:

- `GET/POST /api/gateway-apis/groups`: elenco/creazione con nome.
- `PATCH /api/gateway-apis/groups/{id}`: nome e/o `collapsed`.
- `DELETE /api/gateway-apis/groups/{id}`: elimina soltanto il contenitore.
- `PUT /api/gateway-apis/{id}/group`: `{"group_id":"grp_..."}` oppure null.

Le mutazioni richiedono Origin corrispondente e header dashboard come le
altre azioni amministrative. Nomi fino a 80 caratteri, massimo 100 gruppi;
nomi duplicati sono rifiutati. Le chiavi già esistenti partono senza gruppo.
Migrazione additiva del core alla versione 3: tabelle `dashboard_api_groups`
e `dashboard_api_group_members`, senza riscrivere credenziali o profili.
SQLite e PostgreSQL sono supportati; il contratto pubblico resta versione 1.
