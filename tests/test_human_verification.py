import io
import json
from http.client import IncompleteRead
from os import environ
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from werkzeug.datastructures import MultiDict

import server
from screening_fixtures import retain_eligible_submission


class HumanVerificationTests(unittest.TestCase):
    def setUp(self):
        config = patch.dict(server.app.config, {
            "TESTING": True,
            "TURNSTILE_ENABLED": True,
            "TURNSTILE_SITE_KEY": "unit-test-public-key",
            "TURNSTILE_SECRET_KEY": "unit-test-private-key",
            "TURNSTILE_HOSTNAMES": ("study.example", "localhost"),
            "FLASK_SECRET_KEY": "unit-test-session-key",
            "SECRET_KEY": "unit-test-session-key",
        })
        config.start()
        self.addCleanup(config.stop)
        network = patch("server.urlopen")
        self.urlopen = network.start()
        self.addCleanup(network.stop)
        self.urlopen.side_effect = AssertionError("Unexpected verification call")
        self.client = server.app.test_client()
        with server.rooms_lock:
            server.rooms.clear()
            server.screening_submissions.clear()
        self.addCleanup(self.clear_private_state)

    def clear_private_state(self):
        with server.rooms_lock:
            server.rooms.clear()
            server.screening_submissions.clear()

    def upstream(self, payload=None, raw=None):
        if raw is None:
            if payload is None:
                payload = {"success": True, "action": "study-entry", "hostname": "study.example"}
            raw = json.dumps(payload).encode()
        self.urlopen.side_effect = None
        response = io.BytesIO(raw)
        self.urlopen.return_value = response
        return response

    def submit(self, token="unit-test-response-token", **kwargs):
        return self.client.post("/verify-human", data={"cf-turnstile-response": token}, **kwargs)

    def assert_unverified(self):
        with self.client.session_transaction() as browser_session:
            self.assertIsNot(browser_session.get("human_verified"), True)
        self.assertEqual(self.client.get("/").location, "/verify-human")

    def test_all_study_pages_and_posts_are_gated_without_replaying(self):
        for rule in server.app.url_map.iter_rules():
            if rule.endpoint in ("static", "verify_human") or rule.rule.startswith("/api/"):
                continue
            for method in rule.methods - {"OPTIONS"}:
                with self.subTest(path=rule.rule, method=method):
                    response = self.client.open(rule.rule, method=method, data={"decision": "accept"})
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.location, "/verify-human")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.client.session_transaction() as browser_session:
            self.assertEqual(dict(browser_session), {})
        self.assertEqual(server.rooms, {})
        self.urlopen.assert_not_called()

    def test_all_read_and_write_apis_return_json_403_before_study_gates(self):
        for rule in server.app.url_map.iter_rules():
            if not rule.rule.startswith("/api/"):
                continue
            for method in rule.methods - {"OPTIONS", "HEAD"}:
                with self.subTest(path=rule.rule, method=method):
                    response = self.client.open(
                        rule.rule.replace("<code>", "ROOM01"), method=method,
                        json={"participant_id": "test", "text": "Hello"},
                    )
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(set(response.get_json()), {"error", "verification_url"})
                    self.assertEqual(response.get_json()["verification_url"], "/verify-human")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(server.rooms, {})
        self.urlopen.assert_not_called()

    def test_static_exempt_and_unknown_endpoint_remains_404(self):
        response = self.client.get("/static/css/style.css")
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(self.client.get("/does-not-exist").status_code, 404)
        self.assertEqual(self.client.post("/api/unknown").status_code, 404)
        self.urlopen.assert_not_called()

    def test_verification_markup_disclosure_accessibility_and_no_secret(self):
        response = self.client.get("/verify-human")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        html = response.get_data(as_text=True)
        for text in (
            'src="https://challenges.cloudflare.com/turnstile/v0/api.js"',
            'class="cf-turnstile"', 'data-action="study-entry"',
            'data-sitekey="unit-test-public-key"', 'aria-labelledby="verification-title"',
            'action="/verify-human"', "<noscript>", "JavaScript is required",
            "before the study information and consent page", "IP address",
            "https://www.cloudflare.com/privacypolicy/", "does not mean you have consented",
        ):
            self.assertIn(text, html)
        self.assertNotIn("unit-test-private-key", html)
        self.assertNotIn("unit-test-session-key", html)
        self.urlopen.assert_not_called()

    def test_success_uses_bounded_https_post_and_preserves_all_progress(self):
        progress = {
            "consent_accepted": True, "travel_intro_seen": True,
            "travel_screening_complete": server.SCREENING_VERSION,
            "instructions_complete": True, "room_code": "ROOM01",
            "room_participant_id": "first", "room_token": "unit-test-room-token",
            "screening_submission_id": retain_eligible_submission(self.client),
        }
        with self.client.session_transaction() as browser_session:
            browser_session.update(progress)
        self.upstream()
        response = self.submit()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        verification_request = self.urlopen.call_args.args[0]
        self.assertEqual(verification_request.full_url, "https://challenges.cloudflare.com/turnstile/v0/siteverify")
        self.assertEqual(verification_request.get_method(), "POST")
        self.assertEqual(parse_qs(verification_request.data.decode()), {
            "secret": ["unit-test-private-key"], "response": ["unit-test-response-token"],
        })
        self.assertEqual(self.urlopen.call_args.kwargs, {"timeout": 5})
        with self.client.session_transaction() as browser_session:
            for key, value in progress.items():
                self.assertEqual(browser_session[key], value)
            self.assertIs(browser_session["human_verified"], True)
            self.assertEqual(browser_session["human_verification_config"], server._human_verification_fingerprint())
            self.assertNotIn("unit-test-response-token", str(dict(browser_session)))
        self.assertEqual(self.client.get("/start").location, "/consent")
        self.assertEqual(self.client.get("/consent").location, "/instructions")
        self.assertEqual(self.client.get("/room").status_code, 200)
        for key in progress:
            with self.client.session_transaction() as browser_session:
                self.assertEqual(browser_session[key], progress[key])

    def test_verified_session_does_not_repeat_challenge_or_upstream(self):
        self.upstream()
        self.submit()
        for _ in range(3):
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get("/verify-human").location, "/")
            self.assertEqual(self.submit().location, "/")
            # Existing consent gate still applies, not verification.
            response = self.client.get("/api/rooms/ROOM01")
            self.assertNotIn("verification_url", response.get_json())
        self.urlopen.assert_called_once()

    def test_valid_verification_does_not_replay_blocked_consent_post(self):
        self.assertEqual(self.client.post("/consent", data={"decision": "accept"}).location, "/verify-human")
        self.upstream()
        self.submit()
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("consent_accepted", browser_session)
        self.assertEqual(self.client.get("/start").location, "/consent")

    def test_verified_pair_can_match_ready_and_chat_without_another_check(self):
        first = self.client
        second = server.app.test_client()
        for client, participant_id in ((first, "first"), (second, "second")):
            self.upstream()
            self.assertEqual(client.post("/verify-human", data={
                "cf-turnstile-response": "unit-test-response-token",
            }).location, "/")
            retain_eligible_submission(client)
            with client.session_transaction() as browser_session:
                browser_session.update(
                    consent_accepted=True, travel_intro_seen=True,
                    travel_screening_complete=server.SCREENING_VERSION,
                    instructions_complete=True,
                )
            self.assertEqual(client.post("/api/rooms", json={"participant_id": participant_id}).status_code, 200)
        self.assertEqual(first.get("/role-instructions").status_code, 200)
        self.assertEqual(first.post("/role-instructions").status_code, 302)
        self.assertEqual(first.get("/dialogue").location, "/role-instructions")
        second.post("/role-instructions")
        for client, participant_id in ((first, "first"), (second, "second")):
            self.assertEqual(client.get("/dialogue").status_code, 200)
            self.assertTrue(client.get("/api/rooms/ROOM01").get_json()["chat_ready"])
            for _ in range(2):
                self.assertEqual(client.post("/api/rooms/ROOM01/messages", json={
                    "participant_id": participant_id, "text": "Hello",
                }).status_code, 201)
                self.assertEqual(client.get("/api/rooms/ROOM01/messages", query_string={
                    "participant_id": participant_id,
                }).status_code, 200)
        self.assertEqual(self.urlopen.call_count, 2)

    def test_verification_and_start_preserve_screenout(self):
        with self.client.session_transaction() as browser_session:
            browser_session.update(consent_accepted=True, travel_screened_out=True)
        self.upstream()
        self.submit()
        self.client.get("/start")
        self.assertEqual(self.client.get("/travel-intro").location, "/screened-out-cities")
        with self.client.session_transaction() as browser_session:
            self.assertIs(browser_session["travel_screened_out"], True)

    def test_no_external_next_redirect(self):
        self.upstream()
        response = self.client.post("/verify-human?next=https://outside.example", data={
            "cf-turnstile-response": "unit-test-response-token", "next": "//outside.example",
        })
        self.assertEqual(response.location, "/")

    def test_missing_empty_whitespace_long_and_duplicate_tokens_never_call_upstream(self):
        for data in (
            {}, {"cf-turnstile-response": ""}, {"cf-turnstile-response": "  "},
            {"cf-turnstile-response": "x" * 2049},
            MultiDict([("cf-turnstile-response", "a"), ("cf-turnstile-response", "b")]),
        ):
            with self.subTest(data_length=len(str(data))):
                response = self.client.post("/verify-human", data=data)
                self.assertEqual(response.status_code, 400)
                self.assertIn(b'role="alert"', response.data)
                self.assertIn(b'class="cf-turnstile"', response.data)
                self.assert_unverified()
        self.urlopen.assert_not_called()

    def test_maximum_token_length_is_accepted_for_validation(self):
        self.upstream()
        self.assertEqual(self.submit("x" * 2048).location, "/")
        self.urlopen.assert_called_once()

    def test_failed_or_wrong_host_or_action_never_grants_access(self):
        for result in (
            {"success": False},
            {"success": True, "action": "other-action", "hostname": "study.example"},
            {"success": True, "action": "study-entry", "hostname": "outside.example"},
            {"success": True, "action": "study-entry", "hostname": "study.example.outside.example"},
        ):
            with self.subTest(result=result):
                self.upstream(result)
                self.assertEqual(self.submit().status_code, 400)
                self.assert_unverified()

    def test_request_host_is_not_used_as_allowlist(self):
        self.upstream({"success": True, "action": "study-entry", "hostname": "outside.example"})
        self.assertEqual(self.submit(headers={"Host": "outside.example"}).status_code, 400)
        self.assert_unverified()

    def test_second_explicitly_configured_hostname_is_allowed(self):
        self.upstream({"success": True, "action": "study-entry", "hostname": "localhost"})
        self.assertEqual(self.submit().location, "/")

    def test_malformed_json_wrong_schema_and_nonboolean_success_fail_closed(self):
        for raw in (
            b"not JSON", b"\xff", b"null", b"[]", b"true", b"{}",
            b'{"success":1}', b'{"success":"true"}', b'{"success":null}',
            b'{"success":true}', b'{"success":true,"action":[],"hostname":{}}',
            b"[" * 1100 + b"0" + b"]" * 1100,
        ):
            with self.subTest(raw=raw):
                self.upstream(raw=raw)
                response = self.submit()
                self.assertEqual(response.status_code, 503)
                self.assertIn(b'class="cf-turnstile"', response.data)
                self.assert_unverified()

    def test_network_timeouts_and_read_errors_fail_closed_without_leaking(self):
        for error in (
            URLError("unit-test-private-key unit-test-response-token"),
            TimeoutError("unit-test-private-key unit-test-response-token"),
            OSError("unit-test-private-key unit-test-response-token"),
            HTTPError("https://challenges.cloudflare.com/turnstile/v0/siteverify", 503,
                      "unit-test-private-key unit-test-response-token", {}, None),
            IncompleteRead(b"unit-test-private-key unit-test-response-token"),
        ):
            with self.subTest(error=type(error).__name__):
                self.urlopen.side_effect = error
                response = self.submit()
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(b"unit-test-private-key", response.data)
                self.assertNotIn(b"unit-test-response-token", response.data)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assert_unverified()
        self.urlopen.side_effect = None
        self.urlopen.return_value.__enter__.return_value.read.side_effect = TimeoutError()
        self.assertEqual(self.submit().status_code, 503)
        self.assert_unverified()

    def test_upstream_size_is_bounded_even_for_valid_json(self):
        body = json.dumps({
            "success": True, "action": "study-entry", "hostname": "study.example",
            "padding": "x" * server.TURNSTILE_MAX_RESPONSE_BYTES,
        }).encode()
        self.urlopen.side_effect = None
        reader = self.urlopen.return_value.__enter__.return_value
        reader.read.return_value = body[:server.TURNSTILE_MAX_RESPONSE_BYTES + 1]
        self.assertEqual(self.submit().status_code, 503)
        reader.read.assert_called_once_with(server.TURNSTILE_MAX_RESPONSE_BYTES + 1)
        self.assert_unverified()

    def test_disabled_has_no_widget_or_upstream_and_retains_existing_access_rules(self):
        server.app.config["TURNSTILE_ENABLED"] = False
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/consent").status_code, 200)
        for method in ("GET", "POST"):
            response = self.client.open("/verify-human", method=method, follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b"cf-turnstile", response.data)
            self.assertNotIn(b"challenges.cloudflare.com", response.data)
        self.assertEqual(self.client.get("/room").location, "/consent")
        self.urlopen.assert_not_called()

    def test_enabled_missing_configuration_fails_closed_even_if_previously_verified(self):
        self.upstream()
        self.submit()
        self.urlopen.reset_mock()
        for key, value in (
            ("TURNSTILE_SITE_KEY", ""), ("TURNSTILE_SECRET_KEY", ""),
            ("TURNSTILE_HOSTNAMES", ()), ("FLASK_SECRET_KEY", ""),
            ("SECRET_KEY", "different-generated-development-key"), ("SECRET_KEY", None),
        ):
            with self.subTest(key=key), patch.dict(server.app.config, {key: value}):
                for method, path in (
                    ("GET", "/"), ("GET", "/consent"), ("GET", "/verify-human"),
                    ("POST", "/verify-human"), ("GET", "/api/rooms/ROOM01"),
                    ("POST", "/api/rooms"),
                ):
                    response = self.client.open(path, method=method)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertNotIn(b"unit-test-private-key", response.data)
                    self.assertNotIn(b"cf-turnstile", response.data)
                    if path.startswith("/api/"):
                        self.assertEqual(response.get_json()["verification_url"], "/verify-human")
                static = self.client.get("/static/css/style.css")
                self.assertEqual(static.status_code, 200)
                static.close()
        self.urlopen.assert_not_called()

    def test_site_key_secret_or_allowlist_changes_invalidate_verified_session(self):
        self.upstream()
        self.submit()
        for key, value in (
            ("TURNSTILE_SITE_KEY", "changed-unit-test-public-key"),
            ("TURNSTILE_SECRET_KEY", "changed-unit-test-private-key"),
            ("TURNSTILE_HOSTNAMES", ("other.example",)),
        ):
            with self.subTest(key=key), patch.dict(server.app.config, {key: value}):
                self.assertEqual(self.client.get("/").location, "/verify-human")
                self.assertEqual(self.client.get("/api/rooms/ROOM01").status_code, 403)
        self.urlopen.assert_called_once()

    def test_boolean_marker_without_current_fingerprint_is_not_accepted(self):
        for value in (True, 1, "true"):
            with self.client.session_transaction() as browser_session:
                browser_session["human_verified"] = value
                browser_session.pop("human_verification_config", None)
            self.assertEqual(self.client.get("/").location, "/verify-human")
        with self.client.session_transaction() as browser_session:
            browser_session["human_verification_config"] = server._human_verification_fingerprint()
        self.assertEqual(self.client.get("/").location, "/verify-human")

    def test_success_and_failure_responses_never_expose_secret_or_token(self):
        for success in (False, True):
            self.upstream({"success": success, "action": "study-entry", "hostname": "study.example"})
            response = self.submit()
            browser_visible = response.get_data(as_text=True) + str(response.headers)
            self.assertNotIn("unit-test-private-key", browser_visible)
            self.assertNotIn("unit-test-response-token", browser_visible)

    def test_environment_defaults_parsing_and_no_generated_key_when_enabled(self):
        path = str(Path(server.__file__).resolve())
        with patch.dict(environ, {}, clear=True):
            fresh_app = runpy.run_path(path)["app"]
            self.assertIs(fresh_app.config["TURNSTILE_ENABLED"], False)
            self.assertEqual(fresh_app.test_client().get("/").status_code, 200)
        with patch.dict(environ, {
            "TURNSTILE_ENABLED": "true", "TURNSTILE_SITE_KEY": "unit-test-public-key",
            "TURNSTILE_SECRET_KEY": "unit-test-private-key",
            "TURNSTILE_HOSTNAMES": " study.example, localhost,study.example, ",
        }, clear=True):
            fresh_app = runpy.run_path(path)["app"]
            self.assertEqual(fresh_app.config["TURNSTILE_HOSTNAMES"], ("localhost", "study.example"))
            self.assertIsNone(fresh_app.secret_key)
            self.assertEqual(fresh_app.test_client().get("/").status_code, 503)
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()