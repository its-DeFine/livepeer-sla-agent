# Livepeer SLA Agent - Single container for orchestrators
#
# Build:
#   docker build -t livepeer-sla-agent .
#
# Run agent (connects to dashboard):
#   docker run -d --name sla-agent \
#     -p 9090:9090 \
#     -v ~/.livepeer-sla:/root/.livepeer-sla \
#     -e DASHBOARD_URL=https://your-dashboard.com \
#     livepeer-sla-agent agent
#
# Run dashboard:
#   docker run -d --name sla-dashboard \
#     -p 8080:8080 \
#     -v ./data:/app/data \
#     livepeer-sla-agent dashboard

FROM python:3.12-slim AS base

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY agent/ ./agent/
COPY dashboard/ ./dashboard/

# Install Python dependencies (including eth-account for address linking)
RUN pip install --no-cache-dir ".[eth]"

# Create data directory
RUN mkdir -p /app/data /root/.livepeer-sla /app/data/proofs

# Expose ports
# 9090 = Agent (for verification challenges)
# 8080 = Dashboard
EXPOSE 9090 8080

# Environment variables
ENV DASHBOARD_URL=http://localhost:8080
ENV HEARTBEAT_INTERVAL=60
ENV AGENT_PORT=9090
ENV DASHBOARD_PORT=8080
ENV PYTHONUNBUFFERED=1

# Healthcheck for agent
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${AGENT_PORT}/status || curl -f http://localhost:${DASHBOARD_PORT}/ || exit 1

# Entrypoint script
COPY <<'EOF' /app/entrypoint.sh
#!/bin/bash
set -e

case "$1" in
    agent)
        # Initialize identity if not exists
        if [ ! -f /root/.livepeer-sla/identity.key ]; then
            echo "🔑 Generating new node identity..."
            python -m agent.cli init
        fi

        # Show node ID
        NODE_ID=$(python -c "from agent.identity import get_identity; print(get_identity().node_id)" 2>/dev/null || echo "unknown")

        echo ""
        echo "🚀 Starting Livepeer SLA Agent"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Node ID:     ${NODE_ID:0:32}..."
        echo "  Dashboard:   ${DASHBOARD_URL}"
        echo "  Agent Port:  ${AGENT_PORT}"
        echo "  Heartbeat:   ${HEARTBEAT_INTERVAL}s"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""

        exec python -m agent.cli run \
            --dashboard "${DASHBOARD_URL}" \
            --port "${AGENT_PORT}" \
            --interval "${HEARTBEAT_INTERVAL}"
        ;;

    dashboard)
        echo ""
        echo "📊 Starting Livepeer SLA Dashboard"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Port:        ${DASHBOARD_PORT}"
        echo "  Data:        /app/data"
        echo "  API Docs:    http://localhost:${DASHBOARD_PORT}/docs"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""

        exec python -c "
import uvicorn
from dashboard.server import create_app
app = create_app()
uvicorn.run(app, host='0.0.0.0', port=${DASHBOARD_PORT})
"
        ;;

    init)
        exec python -m agent.cli init "${@:2}"
        ;;

    status)
        exec python -m agent.cli status
        ;;

    attest)
        exec python -m agent.cli attest "${@:2}"
        ;;

    verify)
        exec python -m agent.cli verify "${@:2}"
        ;;

    link)
        # Interactive address linking
        if [ -z "$2" ]; then
            echo "Usage: docker run -it livepeer-sla-agent link <eth_address>"
            echo ""
            echo "Links your ETH orchestrator address to this SLA agent."
            echo "Run with -it for interactive signature input."
            exit 1
        fi

        exec python -m agent.cli link "$2" "${@:3}"
        ;;

    top100)
        # Query top 100 orchestrators
        python -c "
import asyncio
from agent.livepeer import get_livepeer_network

async def main():
    network = get_livepeer_network()
    orchestrators = await network.get_top_orchestrators(20)
    print('Top 20 Livepeer Orchestrators by Stake:')
    print('=' * 60)
    for o in orchestrators:
        stake_k = o.total_stake / 1000
        print(f'  #{o.rank:3d}  {o.eth_address[:20]}...  {stake_k:8.1f}k LPT')
    print('=' * 60)

asyncio.run(main())
"
        ;;

    shell)
        exec /bin/bash
        ;;

    *)
        echo "🎬 Livepeer SLA Agent v0.2.0"
        echo ""
        echo "Usage: docker run livepeer-sla-agent <command>"
        echo ""
        echo "Commands:"
        echo "  agent      Start the SLA agent (for orchestrators)"
        echo "  dashboard  Start the dashboard (for network operators)"
        echo "  init       Generate node identity"
        echo "  status     Show node capabilities"
        echo "  link       Link ETH address (interactive)"
        echo "  attest     Generate signed attestation"
        echo "  verify     Verify an attestation file"
        echo "  top100     Query top 100 orchestrators"
        echo "  shell      Open bash shell"
        echo ""
        echo "Environment variables:"
        echo "  DASHBOARD_URL       Dashboard URL (default: http://localhost:8080)"
        echo "  HEARTBEAT_INTERVAL  Heartbeat interval in seconds (default: 60)"
        echo "  AGENT_PORT          Agent server port (default: 9090)"
        echo "  DASHBOARD_PORT      Dashboard server port (default: 8080)"
        echo ""
        echo "Examples:"
        echo "  # Run agent connecting to production dashboard"
        echo "  docker run -d -p 9090:9090 -v ~/.livepeer-sla:/root/.livepeer-sla \\"
        echo "    -e DASHBOARD_URL=https://sla.livepeer.network livepeer-sla-agent agent"
        echo ""
        echo "  # Link your ETH orchestrator address (interactive)"
        echo "  docker run -it -v ~/.livepeer-sla:/root/.livepeer-sla livepeer-sla-agent link 0xYourAddress"
        echo ""
        echo "  # Run local dashboard"
        echo "  docker run -d -p 8080:8080 -v ./data:/app/data livepeer-sla-agent dashboard"
        ;;
esac
EOF

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["agent"]
