# Livepeer SLA Agent

**A free, open-source alternative to expensive SLA monitoring proposals.**

Built in a weekend. Covers the MVP slice: on-chain activity + signed heartbeats + basic verification, with near-zero infra cost (public Arbitrum RPC).

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
  -e AGENT_PUBLIC_URL=http://<your-host>:9090 \
  ghcr.io/its-define/livepeer-sla-agent:main
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
| Capability | Challenge-response jobs | "I can complete the test job under these settings" |

**No special hardware required.** The active verification approach tests actual capability through challenge-response, which is more trustworthy than passive attestation.

### Security Notes (Read This)

- **Self-reported metrics can be faked by the node operator.** Signatures prove *who* sent the data (key control), not that the hardware metrics are truthful.
- **ETH address linking proves wallet ownership**, but does not prove the agent is running on the same machine that serves Livepeer traffic.
- **Verification endpoints are an attack surface** if exposed publicly; restrict access (firewall/VPN) and avoid running with public `9090` open on the internet.
- For extra protection, set `SLA_CHALLENGE_TOKEN` on both agent + dashboard to require `X-SLA-Token` for `/challenge/*`.

## Commands

### Agent (Run on Orchestrators)

```bash
IMAGE=ghcr.io/its-define/livepeer-sla-agent:main

# Initialize identity
docker run --rm -v ~/.livepeer-sla:/root/.livepeer-sla "$IMAGE" init

# Show capabilities
docker run --rm -v ~/.livepeer-sla:/root/.livepeer-sla "$IMAGE" status

# Run agent (foreground)
docker run -p 9090:9090 \
  -v ~/.livepeer-sla:/root/.livepeer-sla \
  -e DASHBOARD_URL=http://dashboard:8080 \
  "$IMAGE" agent

# Generate one-off attestation
docker run --rm -v ~/.livepeer-sla:/root/.livepeer-sla "$IMAGE" attest
```

### Dashboard (Run once for the network)

```bash
IMAGE=ghcr.io/its-define/livepeer-sla-agent:main
docker run -d -p 8080:8080 -v ./data:/app/data "$IMAGE" dashboard
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
| `/challenge/gpu-benchmark` | POST | Handle GPU benchmark challenge |
| `/gpu-info` | GET | GPU inventory + supported benchmarks |
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
| `AGENT_PUBLIC_URL` | unset | Public agent URL to register for verification (e.g. `http://x.x.x.x:9090`) |
| `SLA_CHALLENGE_TOKEN` | unset | Shared token required for `/challenge/*` endpoints (set on both agent + dashboard) |
| `MAX_CHALLENGE_DOWNLOAD_BYTES` | `52428800` | Max bytes agent will download for transcode challenge |
| `ALLOW_UNSAFE_INPUT_URLS` | unset | Set to `1` to bypass SSRF URL guard (not recommended) |
| `ALLOW_PRIVATE_AGENT_URLS` | unset | Set to `1` to allow private/loopback agent URLs for endpoint registration (not recommended) |
| `PAYMENTS_BACKEND_URL` | unset | Payments-backend base URL (enables paying for verified challenges) |
| `PAYMENTS_ADMIN_TOKEN` | unset | Payments admin token used by the dashboard to credit workloads |
| `PAYMENTS_PAYOUT_LIVENESS_ETH` | unset | ETH payout for successful `liveness` verification |
| `PAYMENTS_PAYOUT_TRANSCODE_ETH` | unset | ETH payout for successful `transcode` verification |
| `PAYMENTS_PAYOUT_GPU_BENCHMARK_ETH` | unset | ETH payout for successful `gpu_benchmark` verification |

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
| **Dependencies** | Multiple external services | Public Arbitrum RPC (+ Livepeer Explorer API fallback) |
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
