"""Private certificate and dashboard expiry notices. Never polls Telegram."""

import argparse
import hashlib
import http.client
import json
import math
import os
import socket
import sqlite3
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

UTC = timezone.utc


def certificate(value, der):
    return {
        "expires_at": datetime.fromtimestamp(ssl.cert_time_to_seconds(value["notAfter"]), UTC).isoformat(),
        "fingerprint_sha256": hashlib.sha256(der).hexdigest(),
    }


def inspect_certificates(config):
    parsed = urlsplit(config["https_url"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Use an HTTPS origin without credentials")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=config["ca_file"])
    roots = context.get_ca_certs()
    binary_roots = context.get_ca_certs(binary_form=True)
    if len(roots) != 1:
        raise ValueError("Use the dedicated single public CA certificate")
    result = {"root": certificate(roots[0], binary_roots[0])}
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=10) as connection, context.wrap_socket(connection, server_hostname=parsed.hostname) as secure:
            result["server"] = certificate(secure.getpeercert(), secure.getpeercert(binary_form=True))
        result["status"] = "verified"
    except ssl.SSLCertVerificationError:
        result["status"] = "certificate_verification_failed"
    except (OSError, ssl.SSLError):
        # A switched-off PC is normal: do not send periodic offline notices.
        result["status"] = "unavailable"
    return result


def expiry_notice(kind, cert, now, origin):
    expires = datetime.fromisoformat(cert["expires_at"])
    seconds = (expires - now).total_seconds()
    stage = next((days for days in (0, 1, 7, 30, 90) if seconds <= days * 86400), None)
    if stage is None:
        return None
    if kind == "dashboard":
        action = (
            "Prima della scadenza, sul server OmniProxy eseguire localmente scripts.extend_operator_session con il file operatore privato e --years 5, "
            "poi aggiornare i metadati del promemoria sull’host dei promemoria. Se il token è già scaduto, usare scripts.provision_identity "
            "per una nuova sessione della stessa identità. Non inviare mai il token su Telegram."
        )
        text = (
            f"Casa degli Agenti — accesso dashboard OmniProxy\n\n"
            f"{'SCADUTO' if seconds <= 0 else 'Promemoria scadenza'}: token operatore della dashboard.\n"
            f"Servizio: {origin}\nScadenza: {expires.astimezone(ZoneInfo('Europe/Rome')):%d/%m/%Y %H:%M %Z}. "
            f"Giorni rimanenti: {max(0, math.ceil(seconds / 86400))}.\n\n"
            "Questo token consente di amministrare OmniProxy. È distinto dal certificato HTTPS e dalle chiavi delle applicazioni AI; "
            "la sua scadenza impedisce l'accesso alla dashboard. Non viene rinnovato automaticamente.\n\n"
            f"Cosa fare: {action}\n\n"
            "Guida sul server OmniProxy: docs/wiki/sicurezza-https.md. "
            "Il promemoria usa soltanto identificativo e scadenza registrati, senza possedere il token."
        )
        return f"expiry:dashboard:{cert['session_id']}:{expires.isoformat()}:{stage}", text
    label = "certificato HTTPS del server" if kind == "server" else "certificato radice (CA) installato sui dispositivi"
    remaining = max(0, math.ceil(seconds / 86400))
    action = (
        "Caddy dovrebbe rinnovarlo automaticamente: verificare che OmniProxy e tls-proxy siano avviati, controllare data/ora e log, "
        "e confermare un nuovo certificato con la CA attendibile. Se la CA non cambia, non serve reinstallarla sui client."
        if kind == "server" else
        "Preparare una nuova CA e i certificati del server; esportare soltanto il certificato pubblico, verificarne l'impronta "
        "e installarlo tra le radici attendibili dei client Windows, Node, Python e container prima della scadenza. Non condividere chiavi private."
    )
    text = (
        f"Casa degli Agenti — OmniProxy sul server OmniProxy\n\n"
        f"{'SCADUTO' if seconds <= 0 else 'Promemoria scadenza'}: {label}.\n"
        f"Servizio: {origin}\nScadenza: {expires.astimezone(ZoneInfo('Europe/Rome')):%d/%m/%Y %H:%M %Z}. "
        f"Giorni rimanenti: {remaining}.\n\n"
        "Questo certificato protegge la comunicazione HTTPS con OmniProxy. È distinto dal token di accesso alla dashboard e dalle chiavi API.\n\n"
        f"Cosa fare: {action}\n\n"
        "Procedura nel repository OmniProxy: docs/wiki/sicurezza-https.md. "
        "Non disattivare la verifica TLS per aggirare l'errore."
    )
    key = f"expiry:{kind}:{cert['fingerprint_sha256']}:{stage}"
    return key, text


def send_private(config_path, text):
    config = json.loads(Path(config_path).read_text())
    owner = config["owner_chat_id"]
    if type(owner) is not int or owner <= 0:
        raise ValueError("Only the private owner chat is allowed")
    body = json.dumps({"chat_id": owner, "text": text, "disable_web_page_preview": True}).encode()
    request = urllib.request.Request("https://api.telegram.org/bot" + config["bot_token"] + "/sendMessage", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
        message_id = result.get("result", {}).get("message_id")
        if result.get("ok") is not True or type(message_id) is not int or message_id <= 0:
            raise ValueError()
        return message_id
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        raise RuntimeError("Telegram delivery not confirmed; receipt retained, no automatic retry") from None


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("State cannot use a symbolic link")
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.executescript("CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,state TEXT,message_id INTEGER); CREATE TABLE IF NOT EXISTS certificate_cache(kind TEXT PRIMARY KEY,value TEXT);")

    def close(self):
        self.db.close()

    def merge(self, observed):
        result = dict(observed)
        with self.db:
            for kind in ("root", "server"):
                if kind in observed:
                    self.db.execute("INSERT OR REPLACE INTO certificate_cache VALUES(?,?)", (kind, json.dumps(observed[kind])))
                else:
                    row = self.db.execute("SELECT value FROM certificate_cache WHERE kind=?", (kind,)).fetchone()
                    if row:
                        result[kind] = json.loads(row[0])
        return result

    def deliver(self, key, text, sender):
        # Persist before sending. Ambiguous delivery is never blindly repeated.
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO receipts VALUES(?,'uncertain',NULL)", (key,)).rowcount
        if not inserted:
            return "already_recorded"
        try:
            message_id = sender(text)
            if type(message_id) is not int or message_id <= 0:
                raise ValueError("Missing receipt")
        except (OSError, ValueError, RuntimeError):
            return "uncertain"
        with self.db:
            self.db.execute("UPDATE receipts SET state='sent',message_id=? WHERE id=?", (message_id, key))
        return "sent"


def check(config, *, dry_run=False, test_message=False, now=None, probe=inspect_certificates, sender=None):
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Timezone required")
    observed = probe(config)
    store = None if dry_run else Store(config["state_path"])
    try:
        certs = store.merge(observed) if store else observed
        notices = []
        for kind in ("root", "server"):
            if kind in certs:
                notice = expiry_notice(kind, certs[kind], now, config["https_url"])
                if notice:
                    key, text = notice
                    if observed["status"] != "verified":
                        text += "\n\nIl server non è attualmente verificabile: la scadenza server può essere quella dell'ultimo controllo riuscito."
                    notices.append((key, text))
        sessions = config.get("dashboard_sessions", [])
        if not isinstance(sessions, list) or len(sessions) > 20:
            raise ValueError("Use at most 20 dashboard session reminders")
        for session in sessions:
            if set(session) != {"session_id", "expires_at"} or not isinstance(session["session_id"], str) or not session["session_id"].startswith("ses_"):
                raise ValueError("Dashboard reminders require metadata only, never tokens")
            expires = datetime.fromisoformat(session["expires_at"])
            if expires.tzinfo is None:
                raise ValueError("Dashboard expiry requires a timezone")
            notice = expiry_notice("dashboard", session, now, config["https_url"])
            if notice:
                notices.append(notice)
        if observed["status"] == "certificate_verification_failed":
            notices.append(("tls-invalid:" + now.date().isoformat(), "Casa degli Agenti — OmniProxy: verifica del certificato HTTPS fallita. Controllare scadenza, CA installata, indirizzo e data/ora del server. Consultare docs/wiki/sicurezza-https.md nel progetto OmniProxy; non disattivare TLS."))
        if test_message:
            expiry = certs.get("server", {}).get("expires_at", "non disponibile")
            root_expiry = certs["root"]["expires_at"]
            session_key = ":" + hashlib.sha256(json.dumps(sessions, sort_keys=True).encode()).hexdigest()[:16] if sessions else ""
            session_text = "".join(f"\nToken dashboard: {datetime.fromisoformat(item['expires_at']).astimezone(ZoneInfo('Europe/Rome')):%d/%m/%Y %H:%M %Z}. Nessun token segreto è copiato sull’host dei promemoria." for item in sessions)
            notices.append(("setup:" + certs["root"]["fingerprint_sha256"] + ":" + expiry + session_key, (
                f"Casa degli Agenti — promemoria HTTPS OmniProxy attivato.\n\nServizio: {config['https_url']}\n"
                f"Certificato server: {expiry}\nCA sui dispositivi: {root_expiry}{session_text}\n\n"
                "Controllo ogni ora dal server 110. Avvisi privati a 90, 30, 7, 1 giorni e alla scadenza, con spiegazione e procedura. "
                "Caddy rinnova automaticamente il certificato server; se il rinnovo avviene non verrà segnalata una vecchia scadenza. "
                "Se il PC 112 è spento, controllo la CA e l'ultima scadenza server verificata. "
                "La scadenza del token di accesso alla dashboard è distinta e non viene modificata dal rinnovo HTTPS. "
                "Gli avvisi del token seguono i metadati registrati: aggiornarli se il token viene esteso, sostituito o revocato.")))
        results = ["preview" if dry_run else store.deliver(key, text, sender or (lambda value: send_private(config["telegram_config_file"], value))) for key, text in notices]
        return {"status": observed["status"], "certificates": {k: certs[k] for k in ("root", "server") if k in certs}, "notices": results, "dry_run": dry_run}
    finally:
        if store:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-message", action="store_true")
    args = parser.parse_args()
    if args.test_message and not args.once:
        raise ValueError("Setup notice requires --once")
    os.umask(0o077)
    while True:
        try:
            config = json.loads(args.config.read_text())
            result = check(config, dry_run=args.dry_run, test_message=args.test_message)
            print(json.dumps(result), flush=True)
            if not args.dry_run:
                Path(config["state_path"]).parent.joinpath("heartbeat").write_text(str(time.time()))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error):
            # Never log exception URLs: a bot token may be part of a request URL.
            print(json.dumps({"status": "check_failed", "details": "Inspect configuration, public CA and local state; no secret details logged"}), flush=True)
            if args.once:
                raise SystemExit(1) from None
        if args.once or args.dry_run:
            return
        time.sleep(3600)


if __name__ == "__main__":
    main()
