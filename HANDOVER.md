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

### ZK Proofs Assessment (Researched, Not Recommended)

We researched whether zero-knowledge proofs could solve the GPU spoofing problem.

**The Oracle Problem:**
```
ZK proves: "Given input X, output Y is correct"
ZK cannot prove: "Input X came from real hardware"
```

| Approach | Solves Spoofing? | Why |
|----------|------------------|-----|
| ZK-proven nvidia-smi | ❌ No | Input can be faked at kernel level |
| ZK-proven transcode | ✅ Partially | Proves computation, 30-120s overhead |
| TEE + ZK (H100 only) | ✅ Yes | Hardware attestation, ~10% availability |

**Conclusion:** ZK is the wrong tool for this problem. Focus on throughput correlation and stress testing instead.

**If ZK is desired later:** Only for H100+ orchestrators with GPU TEE (DCAP attestation wrapped in SP1/RISC Zero proof).

### GPU Proof-of-Work Verification (NEW - IMPLEMENTED)

Based on the insight that **"you can fake nvidia-smi output, but you can't fake physics"**, we implemented a GPU benchmark verification system.

**Core Principle:**
```
Old: "What GPU do you claim?" → nvidia-smi → can be faked
New: "Prove your GPU can do X in Y time" → actual benchmark → can't fake physics
```

**How It Works:**
1. Dashboard sends random seed to agent
2. Agent runs deterministic GPU benchmark (matrix multiply or transcode)
3. Dashboard receives timing + result hash
4. Dashboard compares timing against GPU performance profiles
5. Trust tier assigned based on results

**Trust Tiers:**
| Tier | Badge | Requirements |
|------|-------|--------------|
| ✅ TESTED | Green | GPU benchmark passed (timing matches claimed GPU) |
| ⚠️ CLAIMED | Yellow | Self-reported only (no benchmark verification yet) |
| ⚡ SUSPECT | Orange | Benchmark ran but timing inconsistent with claimed GPU |
| 🚫 FAILED | Red | Benchmark failed or node unreachable |

**Benchmark Types:**
- `matrix_4096` - 4096x4096 matrix multiply (quick, ~45ms on RTX 4090)
- `matrix_8192` - 8192x8192 matrix multiply (thorough, ~180ms on RTX 4090)
- `transcode_720p` - 720p video transcode (real workload test)
- `transcode_1080p` - 1080p video transcode

**GPU Profiles Database:**
We maintain expected timing ranges for 20+ GPU models:
- NVIDIA GeForce RTX 4090, 4080, 4070 series
- NVIDIA GeForce RTX 3090, 3080, 3070 series
- NVIDIA A100, A6000, A5000 (datacenter)
- NVIDIA RTX A4000, T4, V100

**Why This Helps:**
- Catches fake GPU claims (wrong timing for claimed model)
- Catches software emulation (way too slow)
- Verifiable without special hardware
- Can be run on-demand or periodically

**Limitations:**
- Still can't verify TOTAL capacity when GPUs are busy
- Requires known GPU profiles (unknown GPUs get neutral scores)
- TEE integration would make timing unfakeable (future work)

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
│   ├── challenge.py     # Challenge-response handling (liveness, transcode, GPU benchmark)
│   ├── eth_link.py      # ETH address linking logic
│   ├── onchain.py       # Arbitrum RPC queries
│   ├── server.py        # Agent HTTP API (FastAPI)
│   ├── cli.py           # Typer CLI (init, status, link, run)
│   ├── gpu_benchmark.py # GPU benchmark framework (matrix multiply, transcode) [NEW]
│   └── gpu_profiles.py  # Expected GPU performance baselines [NEW]
├── dashboard/
│   ├── models.py        # Pydantic data models
│   ├── storage.py       # In-memory storage + aggregation
│   ├── verifier.py      # Active verification sender (includes GPU benchmark)
│   ├── proofs.py        # Cryptographic verification
│   └── server.py        # Dashboard HTTP API + UI (includes trust tier display)
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
  ghcr.io/its-define/livepeer-sla-agent:main
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
| `/challenge/gpu-benchmark` | POST | Handle GPU benchmark challenge [NEW] |
| `/gpu-info` | GET | GPU info and benchmark capabilities [NEW] |
| `/heartbeat/force` | POST | Force immediate heartbeat |

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
| `/api/v1/nodes/register-endpoint` | POST | Register agent endpoint (requires node-key signature) |
| `/api/v1/verify` | POST | Send verification challenge |
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
   - JSON persistence is implemented for nodes/attestations/heartbeats/jobs (best-effort)
   - Upgrade to SQLite/PostgreSQL for stronger durability + querying
   - Enable longer-term trend analysis

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

### Recently Implemented (GPU Proof-of-Work Verification)

**Files added/modified:**
| File | Description |
|------|-------------|
| `agent/gpu_benchmark.py` | ~500 lines - Deterministic GPU benchmarks (matrix multiply + transcode) |
| `agent/gpu_profiles.py` | ~400 lines - Expected timing profiles for 20+ GPU models |
| `agent/challenge.py` | Added `handle_gpu_benchmark_challenge()` method |
| `agent/server.py` | Added `/challenge/gpu-benchmark` and `/gpu-info` endpoints |
| `dashboard/verifier.py` | Added `verify_gpu_benchmark()` method with trust tier assignment |
| `dashboard/server.py` | Added trust tier display in UI, GPU benchmark verify endpoint |

**Key design decisions:**
1. **Deterministic benchmarks** - Same seed → same result → verifiable output hash
2. **Two benchmark types** - Matrix multiply (pure compute) + transcode (real workload)
3. **Tolerance-based scoring** - 25% margin for system variance
4. **Trust tiers** - TESTED > CLAIMED > SUSPECT > FAILED

**Testing the GPU benchmark:**
```bash
# From agent side - check GPU info
curl http://localhost:9090/gpu-info

# From dashboard - trigger benchmark verification
curl -X POST http://localhost:8080/api/v1/verify \
  -H "Content-Type: application/json" \
  -d '{"node_id": "<node_id>", "challenge_type": "gpu_benchmark", "benchmark_type": "matrix_4096"}'
```

### What to build next (in priority order)

1. **Add TEE timing protection (Phase 4 from plan)** - Wrap benchmarks in Intel SGX for unfakeable timing
2. **Automated periodic verification loop** - Run GPU benchmarks on schedule, not just on-demand
3. **Add throughput-to-capacity correlation** - Compare benchmark results vs ticket earnings
4. **Historical peak tracking** - Track best benchmark times as evidence of true capacity

### What NOT to attempt
- Per-pipeline metrics (requires go-livepeer changes)
- Full TEE for GPU metric verification (H100 only, ~10% availability)
- Solving the "busy GPUs" problem completely (intractable without cooperation)

### The honest position
- **TESTED** = GPU benchmark passed (timing matches claimed GPU model)
- **CLAIMED** = Self-reported only (no benchmark verification yet)
- On-chain data and active challenges are our trust anchors
- This is a $0 alternative that covers much of orchestrator-side SLA monitoring
