import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WRAPPER_PATH = ROOT / "deploy/cloud/scripts/vaa-state-relay.py"


def load_wrapper():
    spec = importlib.util.spec_from_file_location("vaa_state_relay", WRAPPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("state relay wrapper could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload_of_size(size: int) -> bytes:
    prefix = b'{"deviceId":"macbook-main","padding":"'
    suffix = b'"}'
    if size < len(prefix) + len(suffix):
        raise ValueError("payload size is too small")
    return prefix + (b"x" * (size - len(prefix) - len(suffix))) + suffix


class FakeResponse:
    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.read_size = None

    def read(self, size: int):
        self.read_size = size
        return b"private response body"


class FakeConnection:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.response = response or FakeResponse()
        self.requests = []
        self.closed = False

    def request(self, method, path, *, body, headers):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class ConnectionFactory:
    def __init__(self, connection: FakeConnection | None = None) -> None:
        self.connection = connection or FakeConnection()
        self.calls = []

    def __call__(self, host, port, *, timeout):
        self.calls.append((host, port, timeout))
        return self.connection


class StateRelayWrapperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wrapper = load_wrapper()

    def token_file(self, directory: str, token: str = "t" * 32) -> Path:
        path = Path(directory) / "relay-token"
        path.write_text(token + "\n", encoding="ascii")
        return path

    def test_valid_payload_posts_exact_bytes_to_fixed_loopback_api(self):
        with tempfile.TemporaryDirectory() as directory:
            token = "server-only-token-" + ("x" * 32)
            token_path = self.token_file(directory, token)
            payload = json.dumps(
                {"deviceId": "macbook-main", "state": {"safe": True}},
                separators=(",", ":"),
            ).encode()
            response = FakeResponse(204)
            connection = FakeConnection(response)
            factory = ConnectionFactory(connection)

            result = self.wrapper.relay(
                "macbook-main",
                stdin=io.BytesIO(payload),
                environ={},
                token_path=token_path,
                connection_factory=factory,
            )

            self.assertEqual(result, 0)
            self.assertEqual(factory.calls, [("127.0.0.1", 8080, 5)])
            self.assertEqual(len(connection.requests), 1)
            method, path, body, headers = connection.requests[0]
            self.assertEqual((method, path, body), ("POST", "/api/computer/state", payload))
            self.assertEqual(headers["Authorization"], f"Bearer {token}")
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(response.read_size, 1025)
            self.assertTrue(connection.closed)

    def test_exact_32_kib_is_accepted_and_next_byte_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = self.token_file(directory)
            accepted_factory = ConnectionFactory()
            self.assertEqual(
                self.wrapper.relay(
                    "macbook-main",
                    stdin=io.BytesIO(payload_of_size(32 * 1024)),
                    environ={},
                    token_path=token_path,
                    connection_factory=accepted_factory,
                ),
                0,
            )
            rejected_factory = ConnectionFactory()
            self.assertEqual(
                self.wrapper.relay(
                    "macbook-main",
                    stdin=io.BytesIO(payload_of_size((32 * 1024) + 1)),
                    environ={},
                    token_path=token_path,
                    connection_factory=rejected_factory,
                ),
                2,
            )
            self.assertEqual(rejected_factory.calls, [])

    def test_invalid_json_shape_device_and_original_command_fail_closed(self):
        cases = (
            (b"", {}, "macbook-main"),
            (b"not-json", {}, "macbook-main"),
            (b"[]", {}, "macbook-main"),
            (b'{"deviceId":"macbook-main","value":NaN}', {}, "macbook-main"),
            (b'{"deviceId":"other-device"}', {}, "macbook-main"),
            (b'{"deviceId":"macbook-main"}', {"SSH_ORIGINAL_COMMAND": "id"}, "macbook-main"),
            (b'{"deviceId":"macbook-main"}', {}, "Bad Device"),
        )
        with tempfile.TemporaryDirectory() as directory:
            token_path = self.token_file(directory)
            for payload, environ, device_id in cases:
                with self.subTest(payload=payload, environ=environ, device_id=device_id):
                    factory = ConnectionFactory()
                    self.assertEqual(
                        self.wrapper.relay(
                            device_id,
                            stdin=io.BytesIO(payload),
                            environ=environ,
                            token_path=token_path,
                            connection_factory=factory,
                        ),
                        2,
                    )
                    self.assertEqual(factory.calls, [])

    def test_token_must_be_one_visible_ascii_line_between_32_and_256(self):
        invalid_tokens = (
            "x" * 31,
            "x" * 257,
            ("x" * 31) + " ",
            ("x" * 31) + "\t",
            ("x" * 32) + "\nsecond-line",
            "密" * 32,
        )
        payload = b'{"deviceId":"macbook-main"}'
        with tempfile.TemporaryDirectory() as directory:
            for index, token in enumerate(invalid_tokens):
                with self.subTest(index=index):
                    token_path = Path(directory) / f"token-{index}"
                    token_path.write_text(token + "\n", encoding="utf-8")
                    factory = ConnectionFactory()
                    self.assertEqual(
                        self.wrapper.relay(
                            "macbook-main",
                            stdin=io.BytesIO(payload),
                            environ={},
                            token_path=token_path,
                            connection_factory=factory,
                        ),
                        2,
                    )
                    self.assertEqual(factory.calls, [])

    def test_http_failure_is_generic_closes_connection_and_prints_nothing(self):
        class FailingConnection(FakeConnection):
            def request(self, method, path, *, body, headers):
                raise RuntimeError(f"{headers['Authorization']} {body!r}")

        with tempfile.TemporaryDirectory() as directory:
            token_path = self.token_file(directory, "private-server-token-" + ("x" * 32))
            connection = FailingConnection()
            output = io.StringIO()
            errors = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = self.wrapper.relay(
                    "macbook-main",
                    stdin=io.BytesIO(b'{"deviceId":"macbook-main","private":"snapshot"}'),
                    environ={},
                    token_path=token_path,
                    connection_factory=ConnectionFactory(connection),
                )
            self.assertEqual(result, 1)
            self.assertTrue(connection.closed)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(errors.getvalue(), "")

    def test_non_success_response_fails_and_connection_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = self.token_file(directory)
            connection = FakeConnection(FakeResponse(401))
            result = self.wrapper.relay(
                "macbook-main",
                stdin=io.BytesIO(b'{"deviceId":"macbook-main"}'),
                environ={},
                token_path=token_path,
                connection_factory=ConnectionFactory(connection),
            )
            self.assertEqual(result, 1)
            self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
