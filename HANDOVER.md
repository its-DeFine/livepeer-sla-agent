# Livepeer SLA Agent - Engineering Handover

## Executive Summary

This project is a **free, open-source alternative to Cloud SPE's $200,000 proposal** for Livepeer orchestrator SLA monitoring. Built in a weekend to demonstrate that most SLA functionality is straightforward to implement.

**Repository:** https://github.com/its-DeFine/livepeer-sla-agent

**Political Context:** This exists to prove that Livepeer doesn't need expensive infrastructure proposals. On-chain ticket redemptions already prove orchestrators did work - we just need to query it.

---

## Background: The Cloud SPE Proposal

Cloud SPE proposed a $200k, 6-month project involving:
- Streamr for decentralized data transport
- ETL pipelines for data processing
- Data warehouse for storage
- Complex multi-service architecture

**Our counter-argument:** Most of this is unnecessary. We built equivalent functionality in a weekend with:
- Direct Arbitrum RPC queries (no API key needed)
- Single Docker container
- Active verification instead of passive reporting

**What we're honest about:** We CAN'T track per-pipeline metrics (latency per job, VMAF scores per segment). That requires instrumentation inside go-livepeer. That's what Cloud SPE's proposal actually addresses.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR SIDE                         │
│                                                              │
│   ┌─────────────────────────────────────────────────────┐   │
│   │              SLA Agent Container                     │   │
│   │                                                      │   │
│   │  • Ed25519 identity (node ID)                       │   │
│   │  • GPU/CPU/memory capability probing                │   │
│   │  • Real-time GPU metrics (util%, temp, power)       │   │
│   │  • Heartbeat publisher (every 60s)                  │   │
│   │  • Challenge responder (liveness + transcode)       │   │
│   │  • ETH address linking (wallet signature proof)     │   │
│   └─────────────────────────────────────────────────────┘   │
│                           │                                  │
│                           │ Signed attestations              │
│                           ▼                                  │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ HTTPS POST
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    DASHBOARD SIDE                            │
│                                                              │
│   ┌─────────────────────────────────────────────────────┐   │
│   │              Dashboard Container                     │   │
│   │                                                      │   │
│   │  • Attestation collector & signature verifier       │   │
│   │  • On-chain ticket redemption queries (Arbitrum)    │   │
│   │  • Node registry & capability aggregation           │   │
│   │  • Verification challenge sender                    │   │
│   │  • Web UI with Chart.js visualizations              │   │
│   └─────────────────────────────────────────────────────┘   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Trust Model (CRITICAL SECTION)

### The Core Philosophy

```
❌ WRONG: Trust what orchestrators report
✅ RIGHT: Verify what orchestrators can actually do
```

### Trust Levels by Data Type

| Data Type | Trust Level | Reasoning |
|-----------|-------------|-----------|
| **On-chain tickets** | ✅ VERIFIED | Direct Arbitrum RPC - cryptographically unfakeable |
| **Transcode capability** | ✅ VERIFIED | We send actual video, measure actual result |
| **Liveness** | ✅ VERIFIED | Challenge-response with signed nonce |
| **GPU count** | ⚠️ CLAIMED | Self-reported via nvidia-smi, can be faked |
| **GPU utilization** | ⚠️ CLAIMED | Informational only, easily spoofed |
| **GPU temperature** | ⚠️ CLAIMED | Informational only |
| **Network throughput** | ⚠️ CLAIMED | Passive measurement, can be faked |

### The GPU Count Problem (UNRESOLVED)

**Scenario:**
```
Orchestrator claims: "I have 30 GPUs"
Reality: Network only uses 2-3 at any given time
Our verification: Tests 1 GPU worth of capacity
Result: We can't verify total capacity when most GPUs are busy
```

**Why this is hard:**
- We can verify AVAILABLE capacity (what's idle)
- We CANNOT verify TOTAL capacity (what they claim to have)
- Stress testing would require massive video workloads

**Current mitigations:**
1. **Economic incentive** - Lying about MORE capacity is self-punishing (get routed more work than you can handle → fail jobs → lose reputation)
2. **Throughput correlation** - Compare claimed GPUs vs ticket earnings over time
3. **Historical peak tracking** - If they've ever processed 30-GPU workload, they probably have it

**Potential solutions (NOT IMPLEMENTED):**

| Solution | Hardware Required | What It Proves |
|----------|-------------------|----------------|
| TPM 2.0 attestation | ~80% of servers | Agent code unmodified (NOT nvidia-smi output) |
| Intel SGX | ~20% of servers | True memory isolation, real attestation |
| AMD SEV | ~10% (EPYC only) | VM memory encryption |
| Coordinated stress test | None | Full capacity (requires scheduling) |

### TEE Assessment

We explicitly chose NOT to require TEE because:
1. Most orchestrators don't have SGX/SEV hardware
2. Active verification (actually testing) is more practical
3. TEE doesn't solve the "busy GPUs" problem anyway

**If TEE is desired later:** TPM 2.0 is most practical (~80% server availability). It proves agent code integrity but NOT that nvidia-smi output is genuine.

### Trust Summary

```
VERIFIED (unfakeable):
  ├── On-chain ticket redemptions
  ├── Challenge-response liveness
  └── Actual transcode test results

CLAIMED (can be spoofed):
  ├── GPU count
  ├── GPU utilization/temp/power
  ├── Memory/CPU stats
  └── Network throughput

DERIVED (computed from verified data):
  ├── Uptime (from heartbeat gaps)
  ├── Verification success rate
  └── Throughput-to-capacity correlation (suggested)
```

---

## SLA Metrics

### 1. On-Chain (Verifiable)

Queried directly from Arbitrum One via `eth_getLogs`:

| Metric | Source | Contract |
|--------|--------|----------|
| Ticket Redemptions | `WinningTicketRedeemed` events | TicketBroker |
| ETH Earned | Event `faceValue` field | TicketBroker |
| Gateway Traffic | Event `sender` field | TicketBroker |
| Active Orchestrators | Event `recipient` field | TicketBroker |

**Contract Address:** `0xa8bb618B1520E284046F3dFc448851A1Ff26e41B` (Arbitrum One)

### 2. Self-Reported (via Heartbeats)

| Metric | Update Frequency | Trust Level |
|--------|-----------------|-------------|
| GPU count | Every 60s | Claimed |
| GPU utilization % | Every 60s | Claimed |
| GPU temperature °C | Every 60s | Claimed |
| GPU power draw W | Every 60s | Claimed |
| Memory utilization | Every 60s | Claimed |
| Network TX/RX Mbps | Every 60s | Claimed |
| Livepeer process status | Every 60s | Claimed |

### 3. Verified (via Challenges)

| Metric | Method | Trust Level |
|--------|--------|-------------|
| Liveness | Sign random nonce | Verified |
| Transcode capability | Actually transcode video | Verified |
| Response latency | Timing measurement | Verified |
| Verification score | 0-100 based on speed | Verified |

### 4. What We DON'T Track (Requires go-livepeer Integration)

These metrics need instrumentation inside the Livepeer runner:
- Per-job transcoding latency
- Per-job error rates
- VMAF/SSIM quality scores per segment
- Internal pipeline metrics

**This is Cloud SPE's actual value proposition** - we're honest that we can't do this part.

---

## Code Structure

```
livepeer-sla-agent/
├── agent/
│   ├── identity.py      # Ed25519 key management, signing
│   ├── capabilities.py  # Hardware detection (GPU, CPU, memory)
│   ├── heartbeat.py     # Periodic attestation publishing
│   ├── challenge.py     # Challenge-response handling
│   ├── eth_link.py      # ETH address linking logic
│   ├── onchain.py       # Arbitrum RPC queries
│   ├── server.py        # Agent HTTP API (FastAPI)
│   └── cli.py           # Typer CLI (init, status, link, run)
├── dashboard/
│   ├── models.py        # Pydantic data models
│   ├── storage.py       # In-memory storage + aggregation
│   ├── verifier.py      # Active verification sender
│   ├── proofs.py        # Cryptographic verification
│   └── server.py        # Dashboard HTTP API + UI
├── Dockerfile           # Multi-command container
├── docker-compose.yml   # Local development stack
└── pyproject.toml       # Python dependencies
```

---

## Running the Project

### One Command Start
```bash
docker-compose up -d
# Dashboard: http://localhost:8080
# Agent: http://localhost:9090
```

### For Production Orchestrators
```bash
docker run -d --gpus all -p 9090:9090 \
  -v ~/.livepeer-sla:/root/.livepeer-sla \
  -e DASHBOARD_URL=https://sla.livepeer.network \
  ghcr.io/livepeer/sla-agent:latest
```

### Link ETH Address (Interactive)
```bash
docker run -it -v ~/.livepeer-sla:/root/.livepeer-sla \
  livepeer-sla-agent link 0xYourOrchestratorAddress
```

---

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

### Dashboard Endpoints (port 8080)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard UI |
| `/api/v1/attestations` | POST | Submit attestation |
| `/api/v1/nodes` | GET | List all nodes |
| `/api/v1/nodes/{id}` | GET | Node details |
| `/api/v1/network/redemptions` | GET | On-chain ticket data |
| `/api/v1/gateways` | GET | Gateway traffic summary |
| `/api/v1/link/submit` | POST | Submit ETH address link |
| `/api/v1/stats` | GET | Network statistics |

---

## Suggested Next Iterations

### High Priority

1. **Throughput-to-capacity correlation**
   - Compare claimed GPU count vs actual ticket throughput
   - Flag orchestrators whose claims don't match output
   - This partially addresses the GPU count spoofing problem

2. **Historical peak tracking**
   - Track maximum concurrent jobs per node
   - Use as evidence of actual capacity
   - Display "peak verified" vs "claimed" capacity

3. **Automated verification loop**
   - Periodically challenge random orchestrators
   - Build trust scores over time
   - Flag orchestrators that fail challenges

### Medium Priority

4. **TPM attestation (optional)**
   - For operators who want to prove agent integrity
   - Remote attestation via TPM 2.0
   - Proves unmodified agent, not GPU metrics

5. **VMAF quality scoring**
   - Add quality measurement to transcode challenges
   - Track quality over time
   - Compare orchestrators by output quality

6. **Dashboard persistence**
   - Currently in-memory
   - Add SQLite/PostgreSQL for historical data
   - Enable trend analysis

### Lower Priority

7. **Alerts/notifications**
   - Webhook on orchestrator going offline
   - Alert on verification failures
   - Slack/Discord integration

8. **Multi-dashboard federation**
   - Let agents report to multiple dashboards
   - Decentralized SLA monitoring

---

## Open Questions for Next Engineer

1. **Should we implement throughput-to-capacity correlation?**
   - Would help catch inflated GPU claims
   - Compare ticket earnings vs claimed capacity

2. **Is TPM attestation worth adding?**
   - ~80% of servers have TPM 2.0
   - Proves agent integrity, not GPU metrics
   - Is it valuable enough?

3. **How to handle the "busy GPUs" problem?**
   - Can verify available capacity, not total
   - Coordinated stress tests during idle periods?
   - Just be honest and label as "claimed"?

4. **Dashboard persistence strategy?**
   - SQLite for simplicity?
   - PostgreSQL for scale?
   - Time-series DB for metrics?

---

## Dependencies

```toml
dependencies = [
    "fastapi>=0.109.0",
    "uvicorn[standard]",
    "httpx>=0.26.0",
    "pynacl>=1.5.0",        # Ed25519 signatures
    "psutil>=5.9.0",        # System probing
    "pydantic>=2.5.0",
    "rich>=13.7.0",         # CLI output
    "typer>=0.9.0",         # CLI framework
    "eth-abi>=5.0.0",       # Ethereum ABI decoding
    "eth-utils>=4.0.0",
    "eth-account>=0.10.0",  # ETH signature verification
]
```

---

## Summary for Codex Agent

**What to build next (in priority order):**

1. Add throughput-to-capacity correlation to catch GPU count lies
2. Add historical peak tracking as evidence of true capacity
3. Implement automated periodic verification challenges
4. Consider TPM attestation for agent integrity (optional)

**What NOT to attempt:**
- Per-pipeline metrics (requires go-livepeer changes)
- TEE for GPU metric verification (hardware not available)
- Solving the "busy GPUs" problem completely (intractable without cooperation)

**The honest position:**
- Self-reported metrics are CLAIMED, not VERIFIED
- On-chain data and active challenges are our trust anchors
- This is a $0 alternative that does 80% of a $200k proposal
