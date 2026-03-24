import json
import unittest
from unittest.mock import MagicMock, mock_open, patch

from webhook_server import WebhookHandler


class WebhookHandlerTests(unittest.TestCase):
    def setUp(self):
        self.handler = WebhookHandler.__new__(WebhookHandler)
        self.handler._update_pr_status = MagicMock()
        self.handler._get_pr_head_sha = MagicMock(return_value="a" * 40)

    def test_extract_ci_steps(self):
        ci_config = {
            "test": ["docker-compose -f docker/docker-compose.yaml up rule-engine-test"],
            "lint": ["docker-compose -f docker/docker-compose.yaml up lint"],
        }
        steps = self.handler._extract_ci_steps(ci_config)
        self.assertEqual(
            steps,
            [
                ("test", ["docker-compose -f docker/docker-compose.yaml up rule-engine-test"]),
                ("lint", ["docker-compose -f docker/docker-compose.yaml up lint"]),
            ],
        )

    def test_extract_ci_steps_raises_on_invalid_config(self):
        with self.assertRaises(ValueError):
            self.handler._extract_ci_steps({"test": []})

    # First exists() is before clone (no temp dir), second exists() is finally cleanup.
    @patch("webhook_server.os.path.exists", side_effect=[False, True])
    @patch("webhook_server.subprocess.Popen")
    @patch("webhook_server.subprocess.run")
    @patch("webhook_server.open", new_callable=mock_open, read_data='{"test":["cmd1","cmd2"],"lint":["cmd3"]}')
    @patch("webhook_server.uuid.uuid4")
    def test_run_ci_executes_ci_json_steps_with_parallel_commands(
        self, mock_uuid, _mock_file, mock_run, mock_popen, _mock_exists
    ):
        mock_uuid.return_value.hex = "fixedid"
        process_1 = MagicMock()
        process_1.communicate.return_value = (b"ok-1", b"")
        process_1.returncode = 0
        process_2 = MagicMock()
        process_2.communicate.return_value = (b"ok-2", b"")
        process_2.returncode = 0
        process_3 = MagicMock()
        process_3.communicate.return_value = (b"ok-3", b"")
        process_3.returncode = 0
        mock_popen.side_effect = [process_1, process_2, process_3]

        payload = {
            "repository": {
                "name": "rule-engine",
                "clone_url": "https://github.com/temp-noob/rule-engine.git",
                "owner": {"login": "temp-noob"},
            },
        }

        self.handler._run_ci(12, payload)

        mock_run.assert_any_call(
            ["git", "clone", "https://github.com/temp-noob/rule-engine.git", "/tmp/rule-engine-pr-12-fixedid"], check=True
        )
        mock_run.assert_any_call(["git", "fetch", "origin", "pull/12/head:pr-12"], check=True, cwd="/tmp/rule-engine-pr-12-fixedid")
        mock_run.assert_any_call(["git", "checkout", "pr-12"], check=True, cwd="/tmp/rule-engine-pr-12-fixedid")
        mock_run.assert_any_call(["git", "cat-file", "-e", ("a" * 40) + "^{commit}"], check=True, cwd="/tmp/rule-engine-pr-12-fixedid")
        mock_run.assert_any_call(["git", "reset", "--hard", "a" * 40], check=True, cwd="/tmp/rule-engine-pr-12-fixedid")
        mock_popen.assert_any_call(
            ["/bin/bash", "-lc", "cmd1"],
            stdout=unittest.mock.ANY,
            stderr=unittest.mock.ANY,
            cwd="/tmp/rule-engine-pr-12-fixedid",
        )
        mock_popen.assert_any_call(
            ["/bin/bash", "-lc", "cmd2"],
            stdout=unittest.mock.ANY,
            stderr=unittest.mock.ANY,
            cwd="/tmp/rule-engine-pr-12-fixedid",
        )
        mock_popen.assert_any_call(
            ["/bin/bash", "-lc", "cmd3"],
            stdout=unittest.mock.ANY,
            stderr=unittest.mock.ANY,
            cwd="/tmp/rule-engine-pr-12-fixedid",
        )
        self.handler._update_pr_status.assert_any_call(
            "temp-noob", "rule-engine", 12, "pending", "Running CI steps from ci.json...", "a" * 40
        )
        self.handler._update_pr_status.assert_any_call(
            "temp-noob", "rule-engine", 12, "success", "All ci.json steps passed", "a" * 40
        )

    def test_issue_comment_runci_triggers_run_ci(self):
        self.handler._run_ci = MagicMock()
        payload = {
            "action": "created",
            "issue": {"number": 7, "pull_request": {"url": "x"}},
            "comment": {"body": "runci"},
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.handler.headers = {
            "Content-Length": str(len(encoded)),
            "X-GitHub-Event": "issue_comment",
        }
        self.handler.rfile = MagicMock()
        self.handler.rfile.read.return_value = encoded
        self.handler.wfile = MagicMock()
        self.handler._verify_signature = MagicMock(return_value=True)
        self.handler.send_response = MagicMock()
        self.handler.end_headers = MagicMock()

        self.handler.do_POST()

        self.handler._run_ci.assert_called_once_with(7, payload)

    def test_issue_comment_non_runci_does_not_trigger_run_ci(self):
        self.handler._run_ci = MagicMock()
        payload = {
            "action": "created",
            "issue": {"number": 7, "pull_request": {"url": "x"}},
            "comment": {"body": "please run"},
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.handler.headers = {
            "Content-Length": str(len(encoded)),
            "X-GitHub-Event": "issue_comment",
        }
        self.handler.rfile = MagicMock()
        self.handler.rfile.read.return_value = encoded
        self.handler.wfile = MagicMock()
        self.handler._verify_signature = MagicMock(return_value=True)
        self.handler.send_response = MagicMock()
        self.handler.end_headers = MagicMock()

        self.handler.do_POST()

        self.handler._run_ci.assert_not_called()


if __name__ == "__main__":
    unittest.main()
