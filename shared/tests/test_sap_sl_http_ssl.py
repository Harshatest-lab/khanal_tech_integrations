"""SAP SL HTTP SSL context (env-controlled)."""

from __future__ import annotations

import os
import ssl

import pytest

from khanal_shared.sap_sl import http as sap_http


@pytest.fixture
def clear_verify_env() -> None:
    old = os.environ.pop("SAP_SL_VERIFY_SSL", None)
    yield
    if old is not None:
        os.environ["SAP_SL_VERIFY_SSL"] = old
    else:
        os.environ.pop("SAP_SL_VERIFY_SSL", None)


def test_sap_sl_ssl_context_verifies_by_default(clear_verify_env: None) -> None:
    os.environ.pop("SAP_SL_VERIFY_SSL", None)
    ctx = sap_http._sap_sl_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("value", ("false", "0", "no", "off"))
def test_sap_sl_ssl_context_insecure_when_disabled(clear_verify_env: None, value: str) -> None:
    os.environ["SAP_SL_VERIFY_SSL"] = value
    ctx = sap_http._sap_sl_ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


# ── Certificate pinning (SAP_SL_PINNED_SHA256) ───────────────────────────
# The on-prem Service Layer serves a self-signed certificate, so normal CA
# validation can't be turned on. Pinning the exact certificate's SHA-256
# fingerprint is stricter than trusting any certificate and does not depend on
# the certificate carrying a name or IP that matches the address we connect to.


@pytest.fixture
def sap_server(tmp_path):
    """A local HTTPS server with a throwaway self-signed certificate. Yields
    (url, sha256 fingerprint of its certificate)."""
    import hashlib
    import http.server
    import subprocess
    import threading

    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
         "-days", "2", "-subj", "/CN=sap-test-not-matching-the-address"],
        check=True, capture_output=True,
    )
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}/", fingerprint
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def clear_pin_env() -> None:
    old = os.environ.pop("SAP_SL_PINNED_SHA256", None)
    yield
    if old is not None:
        os.environ["SAP_SL_PINNED_SHA256"] = old
    else:
        os.environ.pop("SAP_SL_PINNED_SHA256", None)


def test_pinned_certificate_is_accepted_even_though_verification_is_off(sap_server, clear_verify_env, clear_pin_env) -> None:
    url, fingerprint = sap_server
    os.environ["SAP_SL_VERIFY_SSL"] = "false"  # how prod runs today
    os.environ["SAP_SL_PINNED_SHA256"] = fingerprint

    status, body = sap_http.sap_sl_request_json(sap_http.build_sap_cookie_opener(), "GET", url)

    assert status == 200 and body == {"ok": True}


def test_a_different_certificate_is_refused(sap_server, clear_verify_env, clear_pin_env) -> None:
    """The point of pinning: a machine in the middle presents its own certificate."""
    url, _fingerprint = sap_server
    os.environ["SAP_SL_VERIFY_SSL"] = "false"
    os.environ["SAP_SL_PINNED_SHA256"] = "ab" * 32

    with pytest.raises(OSError, match="pinned fingerprint"):
        sap_http.sap_sl_request_json(sap_http.build_sap_cookie_opener(), "GET", url)


def test_fingerprint_may_be_written_with_colons_and_uppercase(sap_server, clear_verify_env, clear_pin_env) -> None:
    url, fingerprint = sap_server
    os.environ["SAP_SL_VERIFY_SSL"] = "false"
    os.environ["SAP_SL_PINNED_SHA256"] = ":".join(fingerprint[i : i + 2] for i in range(0, 64, 2)).upper()

    status, _body = sap_http.sap_sl_request_json(sap_http.build_sap_cookie_opener(), "GET", url)

    assert status == 200


def test_several_fingerprints_allow_a_certificate_renewal_overlap(sap_server, clear_verify_env, clear_pin_env) -> None:
    url, fingerprint = sap_server
    os.environ["SAP_SL_VERIFY_SSL"] = "false"
    os.environ["SAP_SL_PINNED_SHA256"] = f"{'cd' * 32}, {fingerprint}"

    status, _body = sap_http.sap_sl_request_json(sap_http.build_sap_cookie_opener(), "GET", url)

    assert status == 200


@pytest.mark.parametrize("bad", ("not-a-fingerprint", "ab" * 31, "zz" * 32))
def test_a_malformed_pin_fails_loudly_instead_of_silently_not_pinning(clear_verify_env, clear_pin_env, bad: str) -> None:
    os.environ["SAP_SL_PINNED_SHA256"] = bad
    with pytest.raises(ValueError, match="SAP_SL_PINNED_SHA256"):
        sap_http.build_sap_cookie_opener()


def test_without_a_pin_nothing_changes(sap_server, clear_verify_env, clear_pin_env) -> None:
    url, _fingerprint = sap_server
    os.environ["SAP_SL_VERIFY_SSL"] = "false"

    status, _body = sap_http.sap_sl_request_json(sap_http.build_sap_cookie_opener(), "GET", url)

    assert status == 200


def test_unpinned_insecure_mode_logs_the_fingerprint_it_saw_so_it_can_be_pinned(
    sap_server, clear_verify_env, clear_pin_env, caplog
) -> None:
    """The SAP box only answers from AWS, so the fingerprint to pin can't be read from a laptop. While verification
    is off and nothing is pinned, the connection reports the certificate's fingerprint once per process instead."""
    url, fingerprint = sap_server
    os.environ["SAP_SL_VERIFY_SSL"] = "false"
    sap_http._observed_fingerprints.clear()

    with caplog.at_level("WARNING", logger=sap_http.__name__):
        status, _body = sap_http.sap_sl_request_json(sap_http.build_sap_cookie_opener(), "GET", url)
        sap_http.sap_sl_request_json(sap_http.build_sap_cookie_opener(), "GET", url)  # same certificate again

    assert status == 200
    lines = [r.getMessage() for r in caplog.records if fingerprint in r.getMessage()]
    assert len(lines) == 1  # once, not on every request
    assert "SAP_SL_PINNED_SHA256" in lines[0]


def test_default_verification_does_not_log_or_wrap_the_connection(clear_verify_env, clear_pin_env) -> None:
    os.environ.pop("SAP_SL_VERIFY_SSL", None)
    opener = sap_http.build_sap_cookie_opener()
    assert not any(isinstance(h, sap_http._PinnedHTTPSHandler) for h in opener.handlers)


def test_a_pin_does_not_weaken_default_verification(clear_verify_env, clear_pin_env) -> None:
    os.environ.pop("SAP_SL_VERIFY_SSL", None)
    assert sap_http._sap_sl_ssl_context().verify_mode == ssl.CERT_REQUIRED
