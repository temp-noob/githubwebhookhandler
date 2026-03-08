import unittest
from unittest.mock import MagicMock, patch

from webhook_server import WebhookHandler


class WebhookHandlerTests(unittest.TestCase):
    def setUp(self):
        self.handler = WebhookHandler.__new__(WebhookHandler)
        self.handler._update_pr_status = MagicMock()

    def test_extract_docker_command_from_data_string(self):
        payload = {"data": "docker-compose -f docker/docker-compose.yaml up rule-engine-test"}
        command = self.handler._extract_docker_command(payload)
        self.assertEqual(
            command,
            ["docker-compose", "-f", "docker/docker-compose.yaml", "up", "rule-engine-test"]
        )

    def test_extract_docker_command_from_data_dict(self):
        payload = {"data": {"docker_command": "docker compose up tests"}}
        command = self.handler._extract_docker_command(payload)
        self.assertEqual(command, ["docker", "compose", "up", "tests"])

    @patch("webhook_server.os.path.exists", return_value=False)
    @patch("webhook_server.subprocess.Popen")
    @patch("webhook_server.subprocess.run")
    def test_run_ci_executes_payload_command_at_pr_head(
        self, mock_run, mock_popen, _mock_exists
    ):
        process = MagicMock()
        process.communicate.return_value = (b"ok", b"")
        process.returncode = 0
        mock_popen.return_value = process

        payload = {
            "repository": {
                "name": "rule-engine",
                "clone_url": "https://github.com/temp-noob/rule-engine.git"
            },
            "pull_request": {"head": {"sha": "a" * 40}},
            "data": {"command": "docker-compose -f docker/docker-compose.yaml up rule-engine-test"},
        }

        self.handler._run_ci(12, payload)

        mock_run.assert_any_call(
            ["git", "clone", "https://github.com/temp-noob/rule-engine.git", "/tmp/rule-engine"], check=True
        )
        mock_run.assert_any_call(["git", "fetch", "origin", "pull/12/head:pr-12"], check=True, cwd="/tmp/rule-engine")
        mock_run.assert_any_call(["git", "checkout", "pr-12"], check=True, cwd="/tmp/rule-engine")
        mock_run.assert_any_call(["git", "cat-file", "-e", ("a" * 40) + "^{commit}"], check=True, cwd="/tmp/rule-engine")
        mock_run.assert_any_call(["git", "reset", "--hard", "a" * 40], check=True, cwd="/tmp/rule-engine")
        mock_popen.assert_called_once_with(
            ["docker-compose", "-f", "docker/docker-compose.yaml", "up", "rule-engine-test"],
            stdout=unittest.mock.ANY,
            stderr=unittest.mock.ANY,
            cwd="/tmp/rule-engine",
        )
        self.handler._update_pr_status.assert_any_call(12, "pending", "Running CI command from webhook payload...", "a" * 40)
        self.handler._update_pr_status.assert_any_call(12, "success", "All tests passed!", "a" * 40)

    def test_extract_docker_command_raises_for_missing_data(self):
        with self.assertRaises(ValueError):
            self.handler._extract_docker_command({})

    def test_extract_docker_command_raises_for_non_docker_command(self):
        payload = {"data": {"command": "python -m unittest"}}
        with self.assertRaises(ValueError):
            self.handler._extract_docker_command(payload)

    def test_extract_docker_command_raises_for_absolute_compose_file_path(self):
        payload = {"data": {"command": "docker-compose -f /etc/passwd up tests"}}
        with self.assertRaises(ValueError):
            self.handler._extract_docker_command(payload)

    def test_extract_docker_command_raises_for_parent_path_compose_file(self):
        payload = {"data": {"command": "docker-compose -f docker/../../etc/passwd up tests"}}
        with self.assertRaises(ValueError):
            self.handler._extract_docker_command(payload)


if __name__ == "__main__":
    unittest.main()
