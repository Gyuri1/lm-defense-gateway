# llm-defense-gateway

An **OpenAI-compatible LLM gateway** for `/v1/chat/completions` that inspects every LLM response with **Cisco AI Defense** before it reaches the client.

```
Client (OpenAI SDK, curl, scanner)
        │  Authorization: Bearer <PROXY_API_KEY>
        ▼
llm-defense-gateway  :8100  ──────►  Upstream LLM (OpenAI-compatible, e.g. vLLM)
        │
        ▼
Cisco AI Defense (inspect/chat)
        │
        ├── is_safe = true   → original LLM response is returned unchanged
        └── is_safe = false  → response is replaced with a block message
                               (classifications, rules, event ID)
```

## Features

- Drop-in OpenAI API endpoint (`/v1/chat/completions`, `/v1/models`, `/health`).
- Token authentication for clients, separate from the upstream token.
- Forwards the full request body (`model`, `max_tokens`, `temperature`, ...).
- Cisco AI Defense inspection of the prompt and the answer, using the `is_safe` and `classifications` keys.
- Two block modes: an OpenAI-style answer with `finish_reason=content_filter` (HTTP 200), or an HTTP 403 error.
- Fail-closed by default (configurable fail-open).
- Streaming support (`stream=true`).
- Optional HTTP proxy for the AI Defense call. Upstream LLM calls always connect directly.
- Optional default `max_tokens` cap to keep response times predictable.
- Structured logs with timing for the LLM, AI Defense and the total request.

## Requirements

- Linux with systemd (for service mode)
- Python **3.10+**
- An OpenAI-compatible LLM server (vLLM, Ollama with OpenAI API, LM Studio, ...)
- A Cisco AI Defense API key (only needed if inspection is enabled)

## Installation

```bash
# 1. Create a dedicated service user
sudo useradd --system --no-create-home --shell /usr/sbin/nologin llm-defense-gateway

# 2. Clone the repository
sudo git clone https://github.com/Gyuri1/llm-defense-gateway.git /opt/llm-defense-gateway
cd /opt/llm-defense-gateway

# 3. Create a virtual environment and install dependencies
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt

# 4. Create the configuration
sudo cp .env.example .env
sudo nano .env

# 5. Set ownership and permissions
sudo chown -R llm-defense-gateway:llm-defense-gateway /opt/llm-defense-gateway
sudo chmod 600 /opt/llm-defense-gateway/.env
```

### Installing behind a corporate HTTP proxy

pip does not detect proxies automatically. Pass the proxy explicitly:

```bash
sudo ./venv/bin/pip install --proxy http://<proxy-host>:<port> -r requirements.txt
```

Alternatively, export the proxy variables in your shell. Keep internal networks in `no_proxy`:

```bash
export http_proxy=http://<proxy-host>:<port>
export https_proxy=$http_proxy
export no_proxy=localhost,127.0.0.1,::1,<internal-networks>
export HTTP_PROXY=$http_proxy HTTPS_PROXY=$https_proxy NO_PROXY=$no_proxy
```

> `sudo` drops these variables by default. Use `sudo -E` or the `--proxy` option.

## Configuration

All settings are environment variables. They are read from `.env` (next to the script) or from the systemd `EnvironmentFile`.

| Variable | Default | Description |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | Interface to bind to. |
| `LISTEN_PORT` | `8100` | TCP port of the gateway. |
| `PROXY_API_KEY` | *(required)* | Token that clients must send. |
| `UPSTREAM_BASE_URL` | `http://127.0.0.1:8000/v1` | Base URL of the upstream LLM (must end with `/v1`). |
| `UPSTREAM_API_KEY` | *(empty)* | Bearer token for the upstream LLM. |
| `UPSTREAM_TIMEOUT` | `300` | Upstream timeout in seconds. |
| `DEFAULT_MODEL` | *(empty)* | Model used if the client sends none. |
| `DEFAULT_MAX_TOKENS` | `0` | `max_tokens` applied if the client sends none (`0` = no cap). |
| `RESPONSE_PATH` | `choices[0].message.content` | JSON path of the answer in the upstream response. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `LOG_CONTENT` | `true` | Log the first 80 characters of prompts and answers. Set to `false` to log only their length. |
| `AI_DEFENSE_ENABLED` | `true` | Enable Cisco AI Defense inspection. |
| `AI_DEFENSE_URL` | *(required if enabled)* | AI Defense `inspect/chat` endpoint for your region. |
| `AI_DEFENSE_API_KEY` | *(required if enabled)* | AI Defense API key. |
| `AI_DEFENSE_PROXY` | *(empty = direct)* | HTTP proxy used only for AI Defense calls. |
| `AI_DEFENSE_TIMEOUT` | `30` | AI Defense timeout in seconds. |
| `AI_DEFENSE_FAIL_OPEN` | `false` | If AI Defense is unreachable: `false` returns HTTP 502, `true` returns the uninspected answer. |
| `BLOCK_MODE` | `message` | `message` returns HTTP 200 with a block text. `error` returns HTTP 403. |

> In `.env`, put comments on their own lines. systemd `EnvironmentFile` does not support inline comments.

## Running

### Manually (foreground)

```bash
cd /opt/llm-defense-gateway
./venv/bin/python llm_defense_gateway.py
```

### As a systemd service

```bash
sudo cp llm-defense-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llm-defense-gateway
sudo systemctl status llm-defense-gateway
```

Open the firewall port if needed:

```bash
sudo ufw allow 8100/tcp
# or
sudo firewall-cmd --add-port=8100/tcp --permanent && sudo firewall-cmd --reload
```

## Usage

### curl

```bash
curl http://<gateway-host>:8100/v1/chat/completions \
  -H "Authorization: Bearer <PROXY_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"<model-name>","messages":[{"role":"user","content":"Hello"}]}'
```

### Python (OpenAI SDK)

```bash
pip install openai
export GATEWAY_URL=http://<gateway-host>:8100/v1
export GATEWAY_API_KEY=<PROXY_API_KEY>
export MODEL=<model-name>
python examples/test_client.py
```

### Security scanners and other tools

Configure **API Token** authentication with these values:

| Field | Value |
|---|---|
| API Token Key (header name) | `Authorization` |
| API Token Value | `Bearer <PROXY_API_KEY>` |

Endpoint URL: `http://<gateway-host>:8100/v1/chat/completions`

Request template:

```json
{"messages": [{"content": "{{prompt}}", "role": "user"}], "model": "<model-name>"}
```

Response JSON path: `choices[0].message.content`

The gateway also accepts `Authorization: <token>`, `X-API-Key: <token>` and `API-Key: <token>`.

## AI Defense behavior

The full conversation (system, user and assistant messages) plus the new LLM answer is sent to AI Defense.

**Safe** (`"is_safe": true`): the upstream response is returned unchanged.

**Not safe** (`"is_safe": false`), with `BLOCK_MODE=message`:

```json
{
  "object": "chat.completion",
  "model": "<model-name>",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Response blocked by Cisco AI Defense. Classifications: SAFETY_VIOLATION. Rules: Violence & Public Safety Threats. Event ID: <event-id>"
    },
    "finish_reason": "content_filter"
  }],
  "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32},
  "ai_defense": {
    "is_safe": false,
    "classifications": ["SAFETY_VIOLATION"],
    "rules": ["Violence & Public Safety Threats"],
    "action": "Block",
    "severity": "NONE_SEVERITY",
    "event_id": "<event-id>",
    "explanation": ""
  }
}
```

With `BLOCK_MODE=error`, the client receives HTTP 403 with `error.code = "ai_defense_block"`. The OpenAI SDK raises `PermissionDeniedError`.

## Logs

```bash
journalctl -u llm-defense-gateway -f                     # live
journalctl -u llm-defense-gateway -n 100 --no-pager      # last 100 lines
journalctl -u llm-defense-gateway --since "1 hour ago"
journalctl -u llm-defense-gateway -p warning             # warnings and errors only
```

Typical output:

```
INFO Request from 192.0.2.10 | model=<model-name> | prompt='Write a 300-word essay...'
INFO LLM response in 5.80s | completion_tokens=300 | answer='The ocean, a vast...'
INFO AI Defense in 0.45s | is_safe=True | action=Allow | classifications=[] | rules=[] | event_id=
INFO Total 6.27s | status=200
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Service exits with `Configuration error: ...` | A required variable is missing. | Set it in `.env`, then restart. |
| `ModuleNotFoundError: No module named 'fastapi'` | Dependencies are not installed in the venv, or the wrong Python is used. | Run `./venv/bin/pip install -r requirements.txt`. Check that `ExecStart` points to `venv/bin/python`. |
| pip: `Network is unreachable` | The host is behind an HTTP proxy. | Use `pip install --proxy http://<proxy-host>:<port> ...` |
| `unexpected keyword argument 'proxy'` | httpx is older than 0.26. | Run `./venv/bin/pip install -U "httpx>=0.26"`. |
| HTTP **401** `Invalid API key` | Wrong token or header. | Send `Authorization: Bearer <PROXY_API_KEY>`. The log line `Auth failed ... header names=[...]` shows which headers arrived. |
| Scanner: *credentials do not have permission* | Wrong token key or value in the scanner. | Key: `Authorization`, Value: `Bearer <PROXY_API_KEY>`. |
| HTTP **404** | Wrong path or port. | The URL must be `http://<host>:<LISTEN_PORT>/v1/chat/completions`. |
| Client timeout / `DeadlineExceeded` | Wrong port, host unreachable, or slow generation. | Check the port. Look for `Request from` lines in the log. If requests arrive but are slow, set `DEFAULT_MAX_TOKENS` (e.g. `512`). |
| HTTP **502** `Upstream unreachable` | The gateway cannot reach the LLM. | From the gateway host, run `curl <UPSTREAM_BASE_URL>/models -H "Authorization: Bearer <UPSTREAM_API_KEY>"`. |
| HTTP **502** `AI Defense inspection failed` | Wrong key, wrong regional URL, or no internet or proxy path. | Test with the curl command below. Set `AI_DEFENSE_PROXY` if needed. |
| `journalctl` shows nothing or permission denied | The user lacks journal access. | Use `sudo`, or add the user to the `systemd-journal` group. |

Test AI Defense connectivity from the gateway host:

```bash
curl [-x http://<proxy-host>:<port>] "$AI_DEFENSE_URL" \
  -H "Content-Type: application/json" \
  -H "X-Cisco-AI-Defense-API-Key: $AI_DEFENSE_API_KEY" \
  -d '{"messages":[{"role":"user","content":"Say this is a test!"}]}'
```

The expected result is a JSON verdict containing `"is_safe": true`.

## Security notes

- Never commit `.env`. It is listed in `.gitignore`.
- Use long, random tokens, e.g. `openssl rand -hex 32`.
- The service speaks plain HTTP. For anything beyond a lab, put it behind a TLS reverse proxy (nginx, Caddy, HAProxy) or bind it to an internal interface only.
- Set `LOG_CONTENT=false` if prompts or answers must not be stored in logs.

## License

See [LICENSE](LICENSE).
