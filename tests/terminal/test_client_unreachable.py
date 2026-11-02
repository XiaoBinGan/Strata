"""A failed telemetry connection says which address failed, why, and what to try next.

The cases are produced on this machine only (a closed loopback port, a loopback
socket that accepts and never answers, and a wrapped resolution error): no GPU,
model or outside network is needed.
"""

import io
import json
import socket
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest import mock

from strata_cli.cli import main
from strata_cli.client import ClientError, Endpoint


def _closed_loopback_port() -> int:
    """A port nothing listens on: bound once to have a number, then released."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class UnreachableServerMessageTests(unittest.TestCase):
    def test_connection_refused_names_address_cause_and_next_step(self):
        url = f"http://127.0.0.1:{_closed_loopback_port()}"
        with self.assertRaises(ClientError) as caught:
            Endpoint(url).snapshot()
        message = str(caught.exception)
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn(f"cannot reach Strata server at {url}", message)
        self.assertIn("connection refused", message)
        self.assertIn("strata launch", message)
        self.assertNotIn("URLError", message)

    def test_cli_json_error_carries_the_same_message(self):
        url = f"http://127.0.0.1:{_closed_loopback_port()}"
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["status", "--url", url, "--json"])
        self.assertEqual(code, 3)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], 3)
        self.assertIn(f"cannot reach Strata server at {url}", payload["error"]["message"])
        self.assertIn("connection refused", payload["error"]["message"])

    def test_timeout_names_the_limit_and_the_timeout_flag(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        accepted = []

        def accept_and_say_nothing():
            try:
                accepted.append(listener.accept()[0])
            except OSError:
                pass

        thread = threading.Thread(target=accept_and_say_nothing, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            with self.assertRaises(ClientError) as caught:
                Endpoint(url, timeout=0.25).snapshot()
        finally:
            listener.close()
            thread.join(timeout=2)
            for connection in accepted:
                connection.close()
        message = str(caught.exception)
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn(f"cannot reach Strata server at {url}", message)
        self.assertIn("no answer within 0.25 s", message)
        self.assertIn("--timeout", message)

    def test_resolution_failure_is_named(self):
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))):
            with self.assertRaises(ClientError) as caught:
                Endpoint("https://strata.invalid").snapshot()
        message = str(caught.exception)
        self.assertIn("cannot reach Strata server at https://strata.invalid", message)
        self.assertIn("host name did not resolve", message)
        self.assertIn("--url", message)

    def test_a_remote_refusal_does_not_suggest_starting_a_local_server(self):
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))):
            with self.assertRaises(ClientError) as caught:
                Endpoint("https://strata.example").snapshot()
        message = str(caught.exception)
        self.assertIn("check the server's address and port", message)
        self.assertNotIn("strata launch", message)


if __name__ == "__main__":
    unittest.main()
