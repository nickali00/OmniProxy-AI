import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.https_reminders import Store, check, expiry_notice, send_private

NOW = datetime(2031, 10, 7, 12, tzinfo=timezone.utc)


def cert(days, fingerprint="test-server"):
    return {"expires_at": (NOW + timedelta(days=days)).isoformat(), "fingerprint_sha256": fingerprint}


def config(tmp_path):
    return {"https_url": "https://192.0.2.10", "state_path": str(tmp_path / "state" / "receipts.sqlite3"), "telegram_config_file": str(tmp_path / "unused.json")}


def test_certificate_reminders_reconcile_renewal_and_send_each_threshold_once(tmp_path):
    setup = config(tmp_path)
    sent = []
    state = {"root": cert(1800, "root"), "server": cert(30), "status": "verified"}
    kwargs = {"probe": lambda _: state, "sender": lambda text: sent.append(text) or len(sent), "now": NOW}
    assert check(setup, **kwargs)["notices"] == ["sent"]
    assert check(setup, **kwargs)["notices"] == ["already_recorded"]
    assert "Caddy" in sent[0] and "token" in sent[0] and "Cosa fare" in sent[0]
    assert check(setup, **(kwargs | {"now": NOW + timedelta(days=23)}))["notices"] == ["sent"]
    state["server"] = cert(1826, "renewed")
    assert check(setup, **kwargs)["notices"] == []
    assert len(sent) == 2


def test_offline_pc_uses_last_verified_expiry_without_daily_offline_spam(tmp_path):
    setup = config(tmp_path)
    online = {"root": cert(1800, "root"), "server": cert(91), "status": "verified"}
    assert check(setup, probe=lambda _: online, now=NOW)["notices"] == []
    offline = {"root": cert(1800, "root"), "status": "unavailable"}
    sent = []
    result = check(setup, probe=lambda _: offline, now=NOW + timedelta(days=2), sender=lambda text: sent.append(text) or 1)
    assert result["notices"] == ["sent"]
    assert "ultimo controllo" in sent[0]
    assert check(setup, probe=lambda _: offline, now=NOW + timedelta(days=3))["notices"] == ["already_recorded"]


def test_ambiguous_telegram_delivery_is_persisted_and_never_retried(tmp_path):
    path = tmp_path / "state" / "receipt.sqlite3"
    calls = []

    def ambiguous(text):
        calls.append(text)
        raise TimeoutError("possibly sent")

    store = Store(path)
    assert store.deliver("one", "message", ambiguous) == "uncertain"
    store.close()
    store = Store(path)
    try:
        assert store.deliver("one", "message", ambiguous) == "already_recorded"
        assert len(calls) == 1
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        store.close()


def test_ca_expiry_requires_public_trust_replacement_and_never_mentions_private_tokens():
    notice = expiry_notice("root", cert(7), NOW, "https://192.0.2.10")
    assert notice and "Windows" in notice[1] and "nuova CA" in notice[1]
    assert "Non condividere chiavi private" in notice[1]
    assert expiry_notice("server", cert(91), NOW, "https://192.0.2.10") is None


def test_sender_rejects_group_recipient_and_hides_token_from_errors(tmp_path, monkeypatch):
    path = tmp_path / "bot.json"
    path.write_text(json.dumps({"owner_chat_id": -99, "bot_token": "SECRET-FIXTURE"}))
    with pytest.raises(ValueError, match="private owner"):
        send_private(path, "fixture")
    path.write_text(json.dumps({"owner_chat_id": 99, "bot_token": "SECRET-FIXTURE"}))

    def fail(*args, **kwargs):
        raise OSError("https://api.telegram.org/botSECRET-FIXTURE")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(RuntimeError) as error:
        send_private(path, "fixture")
    assert "SECRET-FIXTURE" not in str(error.value)


def test_preview_sends_nothing_and_writes_no_state(tmp_path):
    setup = config(tmp_path)
    result = check(setup, dry_run=True, now=NOW, probe=lambda _: {"root": cert(1), "status": "unavailable"})
    assert result["notices"] == ["preview"]
    assert not (tmp_path / "state").exists()


def test_tls_failure_alert_is_private_and_deduplicated_per_day(tmp_path):
    setup = config(tmp_path)
    kwargs = {"probe": lambda _: {"root": cert(1800, "root"), "status": "certificate_verification_failed"}, "now": NOW, "sender": lambda _: 1}
    assert check(setup, **kwargs)["notices"] == ["sent"]
    assert check(setup, **kwargs)["notices"] == ["already_recorded"]


def test_dashboard_reminders_work_offline_and_track_expiry_metadata_without_secret(tmp_path):
    setup = config(tmp_path)
    setup["dashboard_sessions"] = [{"session_id": "ses_fixture", "expires_at": cert(30)["expires_at"]}]
    sent = []
    kwargs = {"probe": lambda _: {"root": cert(1800, "root"), "status": "unavailable"}, "now": NOW, "sender": lambda text: sent.append(text) or len(sent)}
    assert check(setup, **kwargs)["notices"] == ["sent"]
    assert check(setup, **kwargs)["notices"] == ["already_recorded"]
    assert "token operatore" in sent[0] and "Non inviare mai il token" in sent[0]
    assert "scripts.extend_operator_session" in sent[0]
    setup["dashboard_sessions"][0]["expires_at"] = cert(1826)["expires_at"]
    assert check(setup, **kwargs)["notices"] == []


def test_dashboard_reminder_configuration_rejects_secret_fields(tmp_path):
    setup = config(tmp_path)
    setup["dashboard_sessions"] = [{"session_id": "ses_fixture", "expires_at": cert(30)["expires_at"], "token": "SECRET-FIXTURE"}]
    with pytest.raises(ValueError, match="metadata only"):
        check(setup, dry_run=True, probe=lambda _: {"root": cert(1800, "root"), "status": "unavailable"}, now=NOW)
