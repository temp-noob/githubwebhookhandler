import unittest
from unittest.mock import MagicMock, patch

from webhook_server import WebhookHandler


class WebhookHandlerTests(unittest.TestCase):
    def setUp(self):
        self.handler = WebhookHandler.__new__(WebhookHandler)
        self.handler._update_pr_status = MagicMock()

    def test_extract_docker_command_from_data_string(self):
        payload = {"data": "docker-compose -f docker/docker-compose.yaml up position-server-test"}
        command = self.handler._extract_docker_command(payload)
        self.assertEqual(
            command,
            ["docker-compose", "-f", "docker/docker-compose.yaml", "up", "position-server-test"]
        )

    def test_extract_docker_command_from_data_dict(self):
        payload = {"data": {"docker_command": "docker compose up tests"}}
        command = self.handler._extract_docker_command(payload)
        self.assertEqual(command, ["docker", "compose", "up", "tests"])

    @patch("webhook_server.os.chdir")
    @patch("webhook_server.os.path.exists", return_value=False)
    @patch("webhook_server.subprocess.Popen")
    @patch("webhook_server.subprocess.run")
    def test_run_ci_checks_out_pr_head_and_runs_payload_command(
        self, mock_run, mock_popen, _mock_exists, _mock_chdir
    ):
        process = MagicMock()
        process.communicate.return_value = (b"ok", b"")
        mock_popen.return_value = process

        payload = {
            "repository": {
                "name": "rule-engine",
                "clone_url": "https://github.com/temp-noob/rule-engine.git"
            },
            "pull_request": {"head": {"sha": "abc123"}},
            "data": {"command": "docker-compose -f docker/docker-compose.yaml up position-server-test"},
        }

        self.handler._run_ci(12, payload)

        mock_run.assert_any_call(["git", "clone", "https://github.com/temp-noob/rule-engine.git", "/tmp/rule-engine"])
        mock_run.assert_any_call(["git", "fetch", "origin", "pull/12/head:pr-12"])
        mock_run.assert_any_call(["git", "checkout", "pr-12"])
        mock_run.assert_any_call(["git", "reset", "--hard", "abc123"])
        mock_popen.assert_called_once_with(
            ["docker-compose", "-f", "docker/docker-compose.yaml", "up", "position-server-test"],
            stdout=unittest.mock.ANY,
            stderr=unittest.mock.ANY,
        )
        self.handler._update_pr_status.assert_any_call(12, "pending", "Running CI command from webhook payload...", "abc123")
        self.handler._update_pr_status.assert_any_call(12, "success", "All tests passed!", "abc123")


if __name__ == "__main__":
    unittest.main()
