# Livepeer SLA Agent

A lightweight attestation agent for Livepeer orchestrators. Provides cryptographic proofs of node capabilities and enables active verification through challenge-response.

## Quick Start (Orchestrators)

**One command to deploy:**

```bash
docker run -d --name livepeer-sla \
  -p 9090:9090 \
  -v ~/.livepeer-sla:/root/.livepeer-sla \
  -e DASHBOARD_URL=https://sla.livepeer.network \
  ghcr.io/livepeer/sla-agent:latest
```

That's it. Your node will:
1. Generate a cryptographic identity (Ed25519 keypair)
2. Probe your hardware capabilities (GPU, CPU, memory)
3. Send signed attestations to the dashboard every 60 seconds
4. Respond to verification challenges to prove capabilities

## How It Works

```
┌─────────────────────────────────────────────────────┐
│                 Your Orchestrator                    │
│  ┌───────────────────────────────────────────────┐  │
│  │            livepeer-sla-agent                 │  │
│  │                                               │  │
│  │  1. Detect capabilities (GPU, CPU, memory)   │  │
│  │  2. Sign attestation with Ed25519 key        │  │
│  │  3. Send to dashboard every N seconds        │  │
│  │  4. Respond to verification challenges       │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
                          │
                          ▼
              ┌───────────────────────────┐
              │        Dashboard          │
              │  - Collects attestations  │
              │  - Verifies signatures    │
              │  - Sends test jobs        │
              │  - Displays network view  │
              └───────────────────────────┘
```

### Trust Model

Instead of trusting what nodes *report*, we **verify** what they can do:

| Layer | Mechanism | What It Proves |
|-------|-----------|----------------|
| Identity | Ed25519 signature | "I control this key" |
| Liveness | Signed timestamp | "I was online at time T" |
| Capability | Active test jobs | "I can transcode at X speed" |

**No special hardware required.** The active verification approach tests actual capability through challenge-response, which is more trustworthy than passive attestation.

## Commands

### Agent (Run on Orchestrators)

```bash
# Initialize identity
docker run --rm -v ~/.livepeer-sla:/root/.livepeer-sla livepeer-sla-agent init

# Show capabilities
docker run --rm -v ~/.livepeer-sla:/root/.livepeer-sla livepeer-sla-agent status

# Run agent (foreground)
docker run -p 9090:9090 \
  -v ~/.livepeer-sla:/root/.livepeer-sla \
  -e DASHBOARD_URL=http://dashboard:8080 \
  livepeer-sla-agent agent

# Generate one-off attestation
docker run --rm -v ~/.livepeer-sla:/root/.livepeer-sla livepeer-sla-agent attest
```

### Dashboard (Run once for the network)

```bash
docker run -d -p 8080:8080 -v ./data:/app/data livepeer-sla-agent dashboard
```

Then visit http://localhost:8080 to see the network dashboard.

## API Reference

### Agent Endpoints (port 9090)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
| `/status` | GET | Agent status and stats |
| `/capabilities` | GET | Current hardware capabilities |
| `/attestation` | GET | Fresh signed attestation |
| `/challenge/liveness` | POST | Handle liveness challenge |
| `/challenge/transcode` | POST | Handle transcode challenge |
| `/heartbeat/force` | POST | Force immediate heartbeat |

### Dashboard Endpoints (port 8080)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard UI |
| `/api/v1/attestations` | POST | Submit attestation |
| `/api/v1/nodes` | GET | List all nodes |
| `/api/v1/nodes/{id}` | GET | Node details |
| `/api/v1/verify` | POST | Send verification challenge |
| `/api/v1/stats` | GET | Network statistics |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_URL` | `http://localhost:8080` | Dashboard to send attestations |
| `HEARTBEAT_INTERVAL` | `60` | Seconds between heartbeats |
| `AGENT_PORT` | `9090` | Agent HTTP port |
| `DASHBOARD_PORT` | `8080` | Dashboard HTTP port |

## Development

```bash
# Install dependencies
pip install -e ".[dev]"

# Run agent locally
python -m agent.cli init
python -m agent.cli status
python -m agent.cli run --dashboard http://localhost:8080

# Run dashboard locally
python -m dashboard.server

# Run tests
pytest
```

## Architecture

```
livepeer-sla-agent/
├── agent/
│   ├── identity.py      # Ed25519 key management
│   ├── capabilities.py  # Hardware detection
│   ├── heartbeat.py     # Periodic attestation publishing
│   ├── challenge.py     # Challenge-response handling
│   ├── server.py        # Agent HTTP API
│   └── cli.py           # Command-line interface
├── dashboard/
│   ├── models.py        # Data models
│   ├── storage.py       # Attestation storage
│   ├── verifier.py      # Active verification
│   └── server.py        # Dashboard HTTP API
├── Dockerfile           # Single container for both
└── docker-compose.yml   # Full stack deployment
```

## Comparison to Cloud SPE Proposal

| Aspect | Cloud SPE ($200k) | This Agent |
|--------|-------------------|------------|
| Timeline | 6 months | Tonight |
| Complexity | Streamr + ETL + Data Warehouse | Single Docker container |
| Trust model | Report-based | Active verification |
| Node deployment | Modify go-livepeer | Single docker run |
| Verification | Passive | Challenge-response |

## License

MIT
