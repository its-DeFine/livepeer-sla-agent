# Livepeer SLA Agent

**A free, open-source alternative to expensive SLA monitoring proposals.**

Built in a weekend. Does 80% of what a $200k proposal promises. Zero infrastructure cost.

This agent provides:
- **On-chain proof of work** - Query ticket redemptions directly from Arbitrum
- **Real-time GPU metrics** - Utilization, temperature, power consumption
- **Cryptographic identity** - Ed25519 signatures for attestations
- **Interactive ETH linking** - Prove wallet ownership via signature
- **Active verification** - Challenge-response to test actual capability

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

## Comparison to $200k Proposals

| Aspect | Expensive Proposal | This Agent |
|--------|-------------------|------------|
| **Cost** | $200,000 | $0 (open source) |
| **Timeline** | 6 months | 1 weekend |
| **On-chain proof** | ❌ Not included | ✅ Direct Arbitrum queries |
| **GPU metrics** | ❌ TBD | ✅ Real-time utilization/temp/power |
| **ETH address linking** | ❌ Not mentioned | ✅ Interactive CLI |
| **Infrastructure** | Streamr + ETL + Data Warehouse | Single Docker container |
| **Dependencies** | Multiple external services | Zero (just public RPC) |
| **Deployment** | Complex integration | `docker run` |
| **Verification** | Passive reporting | Active challenge-response |

### What We Track

**Verifiable (on-chain):**
- Ticket redemptions from TicketBroker contract
- ETH earned per orchestrator
- Gateway → Orchestrator traffic flow
- Active orchestrators receiving work

**Observable (self-reported):**
- GPU utilization % (every 60s)
- GPU temperature °C
- Power consumption (watts)
- CPU, memory, network info

### What We Don't Track (requires go-livepeer integration)

- Per-job transcoding latency
- Per-job error rates
- VMAF/SSIM quality scores
- Internal pipeline metrics

*These require instrumenting inside go-livepeer itself - a separate, smaller project.*

## The Point

On-chain ticket redemptions already prove orchestrators did work. Livepeer doesn't need a $200k "decentralized metrics foundation" - it needs someone to query the data that's already there.

This project exists to demonstrate that.

## License

MIT - Use it, fork it, improve it.
