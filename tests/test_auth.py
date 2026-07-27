"""Tests for the access-control layer in webapp/routes.py: the admin
(local machine)/guest (other private-network peer)/blocked (non-private
address) tiers, guest login/logout, and the Inställningar/Demo
restriction for guest accounts (see routes._enforce_access_control)."""

from signal_events import db as db_module
from signal_events.webapp import create_app

LAN_PEER = "192.168.1.50"
PUBLIC_IP = "8.8.8.8"


def _create_guest(name="Vakt Andersson", password="hemligt123"):
    with db_module.get_connection() as conn:
        db_module.create_user(conn, name, password)


def _login(client, name="Vakt Andersson", password="hemligt123", remote_addr=LAN_PEER):
    return client.post(
        "/login", data={"name": name, "password": password},
        environ_overrides={"REMOTE_ADDR": remote_addr},
    )


def test_admin_tier_has_full_access_by_default():
    """The Flask test client's default REMOTE_ADDR is 127.0.0.1, so every
    existing test in this suite already exercises "admin, unrestricted"
    -- this just makes that assumption explicit."""
    client = create_app().test_client()
    resp = client.get("/settings")
    assert resp.status_code == 200


def test_blocked_tier_gets_403_before_any_login_attempt():
    client = create_app().test_client()
    resp = client.get("/events", environ_overrides={"REMOTE_ADDR": PUBLIC_IP})
    assert resp.status_code == 403


def test_blocked_tier_applies_even_to_the_login_page_itself():
    client = create_app().test_client()
    resp = client.get("/login", environ_overrides={"REMOTE_ADDR": PUBLIC_IP})
    assert resp.status_code == 403


def test_guest_tier_is_redirected_to_login_when_not_authenticated():
    client = create_app().test_client()
    resp = client.get("/events", environ_overrides={"REMOTE_ADDR": LAN_PEER})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_guest_can_log_in_and_reach_an_ordinary_page():
    _create_guest()
    client = create_app().test_client()

    login_resp = _login(client)
    assert login_resp.status_code == 302

    resp = client.get("/events", environ_overrides={"REMOTE_ADDR": LAN_PEER})
    assert resp.status_code == 200


def test_login_rejects_a_wrong_password_without_revealing_which_part_was_wrong():
    _create_guest()
    client = create_app().test_client()

    resp = _login(client, password="fel lösenord")
    assert resp.status_code == 200  # re-renders the login form, doesn't redirect
    assert "Fel namn eller lösenord".encode() in resp.data


def test_login_rejects_an_unknown_user_name():
    client = create_app().test_client()
    resp = _login(client, name="Ingen sådan")
    assert resp.status_code == 200
    assert "Fel namn eller lösenord".encode() in resp.data


def test_guest_cannot_reach_settings_or_demo_tabs():
    _create_guest()
    client = create_app().test_client()
    _login(client)

    settings_resp = client.get("/settings", environ_overrides={"REMOTE_ADDR": LAN_PEER})
    demo_resp = client.get("/events/import/demo", environ_overrides={"REMOTE_ADDR": LAN_PEER})
    demo_clear_resp = client.post(
        "/events/import/demo/clear", environ_overrides={"REMOTE_ADDR": LAN_PEER}
    )

    assert settings_resp.status_code == 403
    assert demo_resp.status_code == 403
    assert demo_clear_resp.status_code == 403


def test_guest_cannot_reach_the_lan_qrcode_route():
    _create_guest()
    client = create_app().test_client()
    _login(client)

    resp = client.get("/settings/lan-qrcode.png", environ_overrides={"REMOTE_ADDR": LAN_PEER})
    assert resp.status_code == 403


def test_guest_nav_hides_settings_and_demo_links_and_shows_who_is_logged_in():
    _create_guest()
    client = create_app().test_client()
    _login(client)

    resp = client.get("/events", environ_overrides={"REMOTE_ADDR": LAN_PEER})

    assert "Inställningar".encode() not in resp.data
    assert "Demo och övning".encode() not in resp.data
    assert "Inloggad som: Vakt Andersson".encode() in resp.data


def test_admin_nav_still_shows_settings_and_demo_links():
    client = create_app().test_client()
    resp = client.get("/events")

    assert "Inställningar".encode() in resp.data
    assert "Demo och övning".encode() in resp.data


def test_logout_clears_the_guest_session():
    _create_guest()
    client = create_app().test_client()
    _login(client)

    client.post("/logout", environ_overrides={"REMOTE_ADDR": LAN_PEER})

    resp = client.get("/events", environ_overrides={"REMOTE_ADDR": LAN_PEER})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_guest_cannot_reach_the_system_log_tab():
    _create_guest()
    client = create_app().test_client()
    _login(client)

    resp = client.get("/system-log", environ_overrides={"REMOTE_ADDR": LAN_PEER})
    assert resp.status_code == 403


def test_admin_nav_shows_the_system_log_tab_but_guest_nav_does_not():
    _create_guest()
    client = create_app().test_client()

    admin_resp = client.get("/events")
    assert "Systemlogg".encode() in admin_resp.data

    _login(client)
    guest_resp = client.get("/events", environ_overrides={"REMOTE_ADDR": LAN_PEER})
    assert "Systemlogg".encode() not in guest_resp.data


def test_system_log_records_login_and_shows_the_active_guest():
    _create_guest()
    client = create_app().test_client()
    _login(client)

    resp = client.get("/system-log")

    assert resp.status_code == 200
    assert "Inloggning".encode() in resp.data
    assert "Vakt Andersson".encode() in resp.data


def test_system_log_records_a_failed_login_attempt():
    client = create_app().test_client()
    _login(client, name="Ingen sådan", password="fel")

    resp = client.get("/system-log")

    assert "Misslyckad inloggning".encode() in resp.data
    assert "Ingen sådan".encode() in resp.data


def test_system_log_records_logout_and_drops_the_active_guest():
    _create_guest()
    client = create_app().test_client()
    _login(client)
    client.post("/logout", environ_overrides={"REMOTE_ADDR": LAN_PEER})

    resp = client.get("/system-log")

    assert "Utloggning".encode() in resp.data
    assert "Inga gäster inloggade just nu".encode() in resp.data


def test_system_log_records_blocked_access_attempts():
    client = create_app().test_client()
    client.get("/events", environ_overrides={"REMOTE_ADDR": PUBLIC_IP})

    resp = client.get("/system-log")

    assert "Åtkomst nekad".encode() in resp.data
    assert PUBLIC_IP.encode() in resp.data
