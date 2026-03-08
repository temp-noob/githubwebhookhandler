## Github Webhook server

The webhook expects GitHub `pull_request` events with JSON payload encoding.

To run CI from webhook payload, include the command in `data`, for example:

```json
{
  "data": {
    "command": "docker-compose -f docker/docker-compose.yaml up position-server-test"
  }
}
```
