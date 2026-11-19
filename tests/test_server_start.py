"""Start der Suchoberfläche ohne Namensabfrage (CI-Hänger vom 16.–19.11.2026)."""
import socket

from belegsuche import server


def test_server_start_fragt_keinen_rechnernamen_ab(monkeypatch):
    # HTTPServer.server_bind() ruft sonst socket.getfqdn() auf; auf dem GitHub-Runner hing das über 25 s.
    def namensabfrage(*args, **kwargs):
        raise AssertionError("getfqdn aufgerufen")

    monkeypatch.setattr(socket, "getfqdn", namensabfrage)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), None)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
        assert httpd.server_name == "127.0.0.1" and httpd.server_port == httpd.server_address[1] > 0
    finally:
        httpd.server_close()
