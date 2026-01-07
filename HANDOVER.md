# Livepeer SLA Agent - Engineering Handover

## Executive Summary

This project is a **lightweight alternative to Cloud SPE's $200,000 proposal** for Livepeer orchestrator SLA monitoring. Instead of an expensive enterprise solution, we built a simple, dockerized agent that orchestrators can run to prove their work and capabilities.

---

## Background: The Cloud SPE Proposal Problem

Cloud SPE proposed a $200k solution for Livepeer network monitoring that would:
- Track orchestrator performance and uptime
- Provide SLA verification
- Monitor network health

**The problem**: This is expensive overkill. Livepeer already has **on-chain proof of work** via ticket redemptions - we just need to aggregate and display it.

**Our solution**: A simple agent (~2000 lines of code) that:
1. Queries on-chain ticket redemptions directly from Arbitrum
2. Lets orchestrators self-report capabilities via signed attestations
3. Verifies orchestrators through challenge-response tests
4. Provides a dashboard for network visibility

---

## What We Built

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR SIDE                         │
│                                                              │
│   ┌─────────────────────────────────────────────────────┐   │
│   │              SLA Agent Container                     │   │
│   │                                                      │   │
│   │  • Ed25519 identity (node ID)                       │   │
│   │  • GPU/CPU/memory capability probing                │   │
│   │  • Heartbeat publisher (every 60s)                  │   │
│   │  • Challenge responder (liveness + transcode)       │   │
│   │  • ETH address linking (prove wallet ownership)     │   │
│   └─────────────────────────────────────────────────────┘   │
│                           │                                  │
│                           │ Signed attestations              │
│                           ▼                                  │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ HTTPS
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    DASHBOARD SIDE                            │
│                                                              │
│   ┌─────────────────────────────────────────────────────┐   │
│   │              Dashboard Container                     │   │
│   │                                                      │   │
│   │  • Attestation collector & verifier                 │   │
│   │  • On-chain ticket redemption queries (Arbitrum)    │   │
│   │  • Node registry & capability aggregation           │   │
│   │  • Verification challenge sender                    │   │
│   │  • Web UI with Chart.js visualizations              │   │
│   └─────────────────────────────────────────────────────┘   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Key Components

| Component | File | Purpose |
|-----------|------|---------|
| **Node Identity** | `agent/identity.py` | Ed25519 keypair for signing attestations |
| **Capability Prober** | `agent/capabilities.py` | Detects GPUs, CPU, memory via nvidia-smi/psutil |
| **Heartbeat Publisher** | `agent/heartbeat.py` | Sends signed capability snapshots to dashboard |
| **Challenge Handler** | `agent/challenge.py` | Responds to liveness/transcode challenges |
| **ETH Linker** | `agent/eth_link.py` | Links node ID to orchestrator ETH address |
| **On-Chain Queries** | `agent/onchain.py` | Queries Arbitrum for ticket redemptions |
| **Dashboard Server** | `dashboard/server.py` | FastAPI server with UI |
| **Storage** | `dashboard/storage.py` | In-memory storage with persistence |
| **Verifier** | `dashboard/verifier.py` | Sends challenges to test nodes |
| **Proofs** | `dashboard/proofs.py` | Cryptographic verification proofs |

---

## SLA Metrics Tracked

### 1. On-Chain (Verifiable Proof of Work)

Queried directly from Arbitrum One via `eth_getLogs`:

| Metric | Source | Description |
|--------|--------|-------------|
| **Ticket Redemptions** | `WinningTicketRedeemed` events | Proof orchestrator did transcoding work |
| **ETH Earned** | Event `faceValue` | How much the orchestrator earned |
| **Gateway Traffic** | Event `sender` field | Which broadcasters sent work |
| **Active Orchestrators** | Event `recipient` field | Who's actually receiving work |

**Contract**: TicketBroker at `0xa8bb618B1520E284046F3dFc448851A1Ff26e41B`

### 2. Self-Reported (via Heartbeats)

| Metric | Update Frequency | Description |
|--------|-----------------|-------------|
| **GPU Utilization %** | Every heartbeat (60s) | Real-time GPU load |
| **GPU Temperature °C** | Every heartbeat | Thermal status |
| **GPU Power Draw W** | Every heartbeat | Power consumption |
| **Memory Utilization** | Every heartbeat | RAM usage |
| **Livepeer Process** | Every heartbeat | Is go-livepeer running? |

### 3. Verified (via Challenges)

| Metric | Method | Description |
|--------|--------|-------------|
| **Liveness** | Sign random challenge | Proves node is online |
| **Transcode Capability** | Actually transcode video | Proves GPU works |
| **Response Latency** | Timing measurement | How fast node responds |
| **Verification Score** | 0-100 based on speed | Performance rating |

---

## Current State

### What Works

- ✅ Docker containers build and run (`docker-compose up -d`)
- ✅ Agent sends heartbeats with GPU metrics
- ✅ Dashboard displays on-chain ticket data
- ✅ Interactive ETH address linking via CLI
- ✅ Chart.js visualizations (earnings trend, traffic flow)
- ✅ Orchestrator/gateway detail pages
- ✅ Direct Arbitrum RPC queries (no API key needed)

### What's Running

```bash
# Check status
docker-compose ps

# Dashboard: http://localhost:8080
# Agent: http://localhost:9090
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/stats` | GET | Network statistics |
| `/api/v1/nodes` | GET | All registered nodes |
| `/api/v1/attestations` | POST | Submit attestation |
| `/api/v1/network/redemptions` | GET | On-chain ticket data |
| `/api/v1/gateways` | GET | Gateway traffic summary |
| `/api/v1/link/submit` | POST | Submit ETH address link |

---

## How to Run

### One Command Start
```bash
docker-compose up -d
```

### Link ETH Address (Interactive)
```bash
docker run -it -v ~/.livepeer-sla:/root/.livepeer-sla \
  livepeer-sla-agent link 0xYourOrchestratorAddress
```

### For Production Orchestrators
```bash
docker run -d --gpus all -p 9090:9090 \
  -v ~/.livepeer-sla:/root/.livepeer-sla \
  -e DASHBOARD_URL=https://sla.livepeer.network \
  livepeer-sla-agent agent
```

---

## Code Changes This Session

### 1. GPU Real-Time Metrics (`agent/capabilities.py`)

**Before**: Cached GPU info, only specs (name, VRAM)
**After**: Fresh probe each heartbeat with utilization, temperature, power

```python
# New nvidia-smi query
"--query-gpu=index,name,memory.total,memory.free,driver_version,utilization.gpu,utilization.memory,temperature.gpu,power.draw"
```

### 2. Direct Arbitrum RPC (`agent/onchain.py`)

**Before**: Used The Graph subgraph (required API key, deprecated free tier)
**After**: Direct `eth_getLogs` to Arbitrum One RPC

```python
RPC_URL = "https://arb1.arbitrum.io/rpc"
TICKET_BROKER_ADDRESS = "0xa8bb618B1520E284046F3dFc448851A1Ff26e41B"
EVENT_SIGNATURE = keccak("WinningTicketRedeemed(address,address,uint256,uint256,uint256,uint256,bytes)")
```

### 3. Dashboard UX (`dashboard/server.py`)

- Added Chart.js for earnings trend visualization
- Hero stat cards (tickets, ETH, orchestrators, gateways)
- Gateway→Orchestrator traffic flow bars
- Color-coded GPU temperature (green/yellow/red)
- Orchestrator and gateway detail pages

### 4. Interactive ETH Linking (`agent/cli.py`)

New `link` command with 3-step flow:
1. Generates message to sign
2. User signs with MEW/MetaMask
3. User pastes signature, CLI submits to dashboard

---

## What's Next (Suggested Iterations)

### High Priority

1. **Uptime Calculation**
   - Calculate % uptime from heartbeat gaps
   - Store historical heartbeat data
   - Display uptime in dashboard

2. **Error Rate Tracking**
   - Track failed verification challenges
   - Calculate success rate per orchestrator
   - Alert on high failure rates

3. **Quality Scores**
   - Add VMAF/SSIM measurement to transcode challenges
   - Store quality metrics over time
   - Compare orchestrators by quality

### Medium Priority

4. **Automated Verification Loop**
   - Periodically challenge random orchestrators
   - Build trust scores over time
   - Flag orchestrators that fail challenges

5. **Historical Data**
   - Store ticket redemptions in database
   - Show earnings over 7/30/90 days
   - Trend analysis

6. **Multi-Dashboard Support**
   - Let agents report to multiple dashboards
   - Federated SLA monitoring

### Nice to Have

7. **Alerts/Notifications**
   - Webhook on orchestrator going offline
   - Alert on GPU temperature threshold
   - Slack/Discord integration

8. **Price/Performance Analysis**
   - ETH earned per GPU hour
   - Compare orchestrator efficiency
   - Network economics dashboard

---

## Repository

**GitHub**: https://github.com/its-DeFine/livepeer-sla-agent

**Latest Commit**: `d602172` on `main`

**Key Files**:
- `Dockerfile` - Single container for agent + dashboard
- `docker-compose.yml` - Local development stack
- `agent/` - Orchestrator-side code
- `dashboard/` - Dashboard-side code
- `pyproject.toml` - Python dependencies

---

## Dependencies

```toml
dependencies = [
    "fastapi>=0.109.0",      # Web framework
    "uvicorn[standard]",      # ASGI server
    "httpx>=0.26.0",          # HTTP client
    "pynacl>=1.5.0",          # Ed25519 signatures
    "psutil>=5.9.0",          # System probing
    "pydantic>=2.5.0",        # Data validation
    "rich>=13.7.0",           # CLI output
    "typer>=0.9.0",           # CLI framework
    "eth-abi>=5.0.0",         # Ethereum ABI decoding
    "eth-utils>=4.0.0",       # Ethereum utilities
    "eth-account>=0.10.0",    # ETH signature verification
]
```

---

## Contact

This handover was prepared for evaluation and further engineering iterations. The codebase is production-ready for initial deployment but would benefit from the suggested iterations above.

**Alternative to Cloud SPE's $200k proposal**: This entire solution is ~2000 lines of Python, runs in Docker, and uses only public Arbitrum RPC endpoints. No expensive infrastructure required.
