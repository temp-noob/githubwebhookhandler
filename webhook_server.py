import json
import hmac
import hashlib
import subprocess
import shlex
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
import os
import requests
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
    filename=os.environ.get('WEBHOOK_LOG_FILE', '/tmp/webhook.log')
)
logger = logging.getLogger('webhook_server')

# Configuration
PORT = 8000
SECRET = os.environ.get('WEBHOOK_SECRET')  # Set this to a secure random string
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
CI_COMMAND_TIMEOUT_SECONDS = int(os.environ.get('CI_COMMAND_TIMEOUT_SECONDS', '3600'))

class WebhookHandler(BaseHTTPRequestHandler):
    _SHA_REGEX = re.compile(r'^[a-fA-F0-9]{40}$')

    def _extract_docker_command(self, payload):
        data = payload.get('data')
        if isinstance(data, str):
            command = data
        elif isinstance(data, dict):
            command = data.get('docker_command') or data.get('command')
        else:
            command = None

        if not command or not isinstance(command, str):
            raise ValueError("Missing docker command in payload data section")

        command_parts = shlex.split(command)
        if not command_parts:
            raise ValueError("Missing docker command in payload data section")

        is_docker_compose = (
            command_parts[0] == 'docker-compose' or
            (command_parts[0] == 'docker' and len(command_parts) > 1 and command_parts[1] == 'compose')
        )
        if not is_docker_compose:
            raise ValueError("Only docker compose commands are allowed in payload data section")

        for i, part in enumerate(command_parts):
            if part in ('-f', '--file') and i + 1 < len(command_parts):
                compose_file = command_parts[i + 1]
                normalized_path = os.path.normpath(compose_file.replace('\\', '/'))
                if os.path.isabs(normalized_path) or normalized_path.startswith('..'):
                    raise ValueError("Compose file path must be relative to repository")

        return command_parts

    def _verify_signature(self, data):
        if 'X-Hub-Signature-256' not in self.headers:
            logger.warning("No signature found in request")
            return False
            
        received_sig = self.headers['X-Hub-Signature-256'].split('sha256=')[1]
        calculated_sig = hmac.new(
            SECRET.encode(), 
            data, 
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(received_sig, calculated_sig)
    
    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        
        # Verify webhook signature
        if not self._verify_signature(post_data):
            logger.warning("Invalid signature")
            self.send_response(403)
            self.end_headers()
            return
            
        # Parse the JSON payload
        event = self.headers.get('X-GitHub-Event')
        payload = json.loads(post_data.decode('utf-8'))
        
        # Log the event type
        logger.info(f"Received GitHub event: {event}")
        
        # Process only pull request events
        if event == 'pull_request':
            pr_action = payload.get('action')
            pr_number = payload.get('number')
            
            logger.info(f"Pull request #{pr_number}, action: {pr_action}")
            
            if pr_action in ['opened', 'synchronize', 'reopened']:
                logger.info(f"Processing PR #{pr_number}, action: {pr_action}")
                self._run_ci(pr_number, payload)
        
        # Respond to GitHub
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Webhook received')
    
    def _update_pr_status(self, pr_number, state, description, commit_sha):
        """Update the PR status in GitHub"""
        if not GITHUB_TOKEN:
            logger.warning("No GitHub token found, skipping status update")
            return
            
        owner = "temp-noob"
        repo = "rule-engine"
        
        # Create the status
        data = {
            'state': state,  # 'pending', 'success', 'error', or 'failure'
            'description': description,
            'context': 'CI/Docker Tests',  # Change the context to make it more visible
            'target_url': f"https://github.com/temp-noob/rule-engine/pull/{pr_number}/checks"
        }
        
        logger.info(f"Updating PR #{pr_number} status to {state}: {description}")
        
        r = requests.post(
            f'https://api.github.com/repos/{owner}/{repo}/statuses/{commit_sha}',
            headers={
                'Authorization': f'token {GITHUB_TOKEN}',
                'Accept': 'application/vnd.github.v3+json'
            },
            json=data
        )
        
        if r.status_code != 201:
            logger.error(f"Failed to update status: {r.content}")
    
    def _run_ci(self, pr_number, payload):
        """Run CI for the specified PR"""
        commit_sha = None
        temp_repo_path = None
        try:
            # Extract repository information from payload
            repo_name = payload['repository']['name']
            repo_url = payload['repository']['clone_url']
            commit_sha = payload['pull_request']['head']['sha']
            if not self._SHA_REGEX.match(commit_sha):
                raise ValueError("Invalid PR head SHA")
            docker_command = self._extract_docker_command(payload)
            
            # Create temp directory path
            temp_repo_path = f"/tmp/{repo_name}"
            
            # Update to pending status
            self._update_pr_status(pr_number, 'pending', 'Running CI command from webhook payload...', commit_sha)
            
            # Check if directory exists and remove it
            if os.path.exists(temp_repo_path):
                logger.info(f"Removing existing directory: {temp_repo_path}")
                subprocess.run(['rm', '-rf', temp_repo_path], check=True)
            
            # Clone the repository
            logger.info(f"Cloning repository to {temp_repo_path}")
            subprocess.run(['git', 'clone', repo_url, temp_repo_path], check=True)
            
            # Fetch the PR
            logger.info(f"Fetching PR #{pr_number}")
            subprocess.run(['git', 'fetch', 'origin', f'pull/{pr_number}/head:pr-{pr_number}'], check=True, cwd=temp_repo_path)
            
            # Checkout the PR branch
            logger.info(f"Checking out PR #{pr_number} branch")
            subprocess.run(['git', 'checkout', f'pr-{pr_number}'], check=True, cwd=temp_repo_path)
            subprocess.run(['git', 'cat-file', '-e', f'{commit_sha}^{{commit}}'], check=True, cwd=temp_repo_path)
            subprocess.run(['git', 'reset', '--hard', commit_sha], check=True, cwd=temp_repo_path)
            
            # Run docker command from webhook payload data section
            logger.info(f"Running CI command from payload data: {' '.join(docker_command)}")

            process = subprocess.Popen(
                docker_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=temp_repo_path
            )
            try:
                stdout, stderr = process.communicate(timeout=CI_COMMAND_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise TimeoutError(
                    f"CI command '{' '.join(docker_command)}' timed out after {CI_COMMAND_TIMEOUT_SECONDS} seconds"
                )
            
            # Log test output
            logger.info(f"Test output:\n{stdout.decode('utf-8', errors='replace')}")
            if stderr:
                logger.error(f"Test errors:\n{stderr.decode('utf-8', errors='replace')}")
                        
            # Update final status
            if process.returncode == 0:
                self._update_pr_status(pr_number, 'success', 'All tests passed!', commit_sha)
            else:
                self._update_pr_status(pr_number, 'failure', 'Tests failed', commit_sha)
            
            logger.info(f"CI for PR #{pr_number} completed with status: {'success' if process.returncode == 0 else 'failure'}")
            
        except Exception as e:
            logger.error(f"Error running CI for PR #{pr_number}: {str(e)}")
            if commit_sha:
                self._update_pr_status(pr_number, 'error', f'CI failed: {str(e)}', commit_sha)
        finally:
            if temp_repo_path and os.path.exists(temp_repo_path):
                try:
                    subprocess.run(['rm', '-rf', temp_repo_path], check=True)
                except Exception as cleanup_error:
                    logger.error(f"Failed to clean temp path {temp_repo_path}: {cleanup_error}")

def run_server():
    server_address = ('', PORT)
    httpd = HTTPServer(server_address, WebhookHandler)
    logger.info(f'Starting webhook server on port {PORT}...')
    httpd.serve_forever()

if __name__ == '__main__':
    run_server()
