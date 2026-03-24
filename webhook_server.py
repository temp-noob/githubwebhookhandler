import json
import hmac
import hashlib
import subprocess
import re
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
import os
import requests
import logging

# Configure logging
logging.basicConfig(
    level=os.getenv('WEBHOOK_LOG_LEVEL', 'INFO').upper(),
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
    
    def _extract_ci_steps(self, ci_config):
        if not isinstance(ci_config, dict) or not ci_config:
            raise ValueError("ci.json must be a non-empty object")
        
        steps = []
        for step_name, commands in ci_config.items():
            if not isinstance(commands, list) or not commands:
                raise ValueError(f"ci.json step '{step_name}' must be a non-empty list")
            parsed_commands = []
            for command in commands:
                if not isinstance(command, str) or not command.strip():
                    raise ValueError(f"ci.json step '{step_name}' contains invalid command")
                parsed_commands.append(command.strip())
            steps.append((step_name, parsed_commands))
        
        return steps

    def _get_pr_head_sha(self, owner, repo, pr_number):
        headers = {'Accept': 'application/vnd.github.v3+json'}
        if GITHUB_TOKEN:
            headers['Authorization'] = f'token {GITHUB_TOKEN}'
        response = requests.get(
            f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}',
            headers=headers
        )
        response.raise_for_status()
        sha = response.json().get('head', {}).get('sha')
        if not isinstance(sha, str) or not self._SHA_REGEX.match(sha):
            raise ValueError("Unable to resolve valid PR head SHA")
        return sha

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
        logger.debug(f"Received POST data: {post_data}")
        
        # Verify webhook signature. Commenting for now.
        # if not self._verify_signature(post_data):
        #     logger.warning("Invalid signature")
        #     self.send_response(403)
        #     self.end_headers()
        #     return
            
        # Parse the JSON payload
        event = self.headers.get('X-GitHub-Event')
        payload = json.loads(post_data.decode('utf-8'))
        
        # Log the event type
        logger.info(f"Received GitHub event: {event}")
        
        # Process only issue comment events with "runci" on pull requests
        if event == 'issue_comment':
            action = payload.get('action')
            comment_body = payload.get('comment', {}).get('body', '').strip().lower()
            issue = payload.get('issue', {})
            pr_number = issue.get('number')
            is_pr_comment = issue.get('pull_request') is not None
            
            logger.info(f"Issue comment action={action}, issue={pr_number}, is_pr_comment={is_pr_comment}")
            
            if action == 'created' and is_pr_comment and comment_body == 'runci':
                logger.info(f"Running CI for PR #{pr_number} due to runci comment")
                self._run_ci(pr_number, payload)
        
        # Respond to GitHub
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Webhook received')
    
    def _update_pr_status(self, owner, repo, pr_number, state, description, commit_sha):
        """Update the PR status in GitHub"""
        if not GITHUB_TOKEN:
            logger.warning("No GitHub token found, skipping status update")
            return
        
        # Create the status
        data = {
            'state': state,  # 'pending', 'success', 'error', or 'failure'
            'description': description,
            'context': 'CI/Docker Tests',  # Change the context to make it more visible
            'target_url': f"https://github.com/{owner}/{repo}/pull/{pr_number}/checks"
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
        owner = None
        repo = None
        try:
            # Extract repository information from payload
            repo_name = payload['repository']['name']
            repo_url = payload['repository']['clone_url']
            owner = payload['repository']['owner']['login']
            repo = repo_name
            commit_sha = self._get_pr_head_sha(owner, repo, pr_number)
            
            # Create temp directory path
            safe_repo_name = re.sub(r'[^A-Za-z0-9_.-]', '-', repo_name)
            temp_repo_path = f"/tmp/{safe_repo_name}-pr-{pr_number}-{uuid.uuid4().hex}"
            
            # Update to pending status
            self._update_pr_status(owner, repo, pr_number, 'pending', 'Running CI steps from ci.json...', commit_sha)
            
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
            
            # Load and execute ci.json steps: steps sequentially, commands in each step in parallel
            ci_config_path = os.path.join(temp_repo_path, 'ci.json')
            try:
                with open(ci_config_path, 'r', encoding='utf-8') as ci_file:
                    ci_steps = self._extract_ci_steps(json.load(ci_file))
            except FileNotFoundError as exc:
                raise FileNotFoundError("ci.json not found in repository root") from exc

            has_failures = False
            for step_name, commands in ci_steps:
                logger.info(f"Running CI step '{step_name}' with {len(commands)} command(s) in parallel")
                processes = []
                for command in commands:
                    logger.info(f"Starting command: {command}")
                    processes.append((
                        command,
                        subprocess.Popen(
                            ['/bin/bash', '-lc', command],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            cwd=temp_repo_path
                        )
                    ))

                for command, process in processes:
                    try:
                        stdout, stderr = process.communicate(timeout=CI_COMMAND_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                        raise TimeoutError(
                            f"CI command '{command}' timed out after {CI_COMMAND_TIMEOUT_SECONDS} seconds"
                        )
                    logger.info(f"Output for {command}:\n{stdout.decode('utf-8', errors='replace')}")
                    if stderr:
                        logger.error(f"Errors for {command}:\n{stderr.decode('utf-8', errors='replace')}")
                    if process.returncode != 0:
                        has_failures = True
                        for _, other_process in processes:
                            if other_process is process:
                                continue
                            if other_process.poll() is None:
                                other_process.kill()
                            other_process.communicate()
                        break
                if has_failures:
                    break

            if has_failures:
                self._update_pr_status(owner, repo, pr_number, 'failure', 'One or more ci.json commands failed', commit_sha)
            else:
                self._update_pr_status(owner, repo, pr_number, 'success', 'All ci.json steps passed', commit_sha)
            
            logger.info(f"CI for PR #{pr_number} completed with status: {'failure' if has_failures else 'success'}")
            
        except Exception as e:
            logger.error(f"Error running CI for PR #{pr_number}: {str(e)}")
            if commit_sha and owner and repo:
                self._update_pr_status(owner, repo, pr_number, 'error', f'CI failed: {str(e)}', commit_sha)
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
