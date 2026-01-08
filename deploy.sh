#!/bin/bash
# Quick deployment script for Livepeer SLA Agent
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/its-DeFine/livepeer-sla-agent/main/deploy.sh | bash
#
# Or with custom dashboard:
#   DASHBOARD_URL=https://sla.livepeer.network ./deploy.sh

set -e

DASHBOARD_URL="${DASHBOARD_URL:-http://localhost:8080}"
CONTAINER_NAME="${CONTAINER_NAME:-livepeer-sla}"
AGENT_PORT="${AGENT_PORT:-9090}"
HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-60}"
AGENT_PUBLIC_URL="${AGENT_PUBLIC_URL:-}"
IMAGE="${IMAGE:-ghcr.io/its-define/livepeer-sla-agent:main}"

echo "🎬 Livepeer SLA Agent Deployment"
echo "================================"
echo ""
echo "Dashboard URL: ${DASHBOARD_URL}"
echo "Agent Port: ${AGENT_PORT}"
echo "Heartbeat: ${HEARTBEAT_INTERVAL}s"
if [ -n "${AGENT_PUBLIC_URL}" ]; then
    echo "Agent Public URL: ${AGENT_PUBLIC_URL}"
else
    echo "Agent Public URL: (not set; active verification may not work)"
fi
echo ""

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker is not running. Please start Docker first."
    exit 1
fi

# Stop existing container if running
if docker ps -q -f name="${CONTAINER_NAME}" | grep -q .; then
    echo "⏹️  Stopping existing container..."
    docker stop "${CONTAINER_NAME}" > /dev/null
    docker rm "${CONTAINER_NAME}" > /dev/null
fi

# Build if local, otherwise pull
if [ -f "Dockerfile" ]; then
    echo "🔨 Building from source..."
    docker build -t "${IMAGE}" . > /dev/null
else
    echo "📥 Pulling latest image..."
    docker pull "${IMAGE}" > /dev/null 2>&1 || {
        echo "Image not found, building locally..."
        echo "Please run this script from the livepeer-sla-agent directory"
        exit 1
    }
fi

# Run the agent
echo "🚀 Starting agent..."
EXTRA_ENVS=()
if [ -n "${AGENT_PUBLIC_URL}" ]; then
    EXTRA_ENVS+=(-e "AGENT_PUBLIC_URL=${AGENT_PUBLIC_URL}")
fi
docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    -p "${AGENT_PORT}:9090" \
    -v ~/.livepeer-sla:/root/.livepeer-sla \
    -e DASHBOARD_URL="${DASHBOARD_URL}" \
    -e HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL}" \
    "${EXTRA_ENVS[@]}" \
    "${IMAGE}" agent > /dev/null

# Wait for startup
sleep 2

# Get node ID (public key). Do NOT read or print the private key material.
NODE_ID=$(
  docker exec "${CONTAINER_NAME}" \
    python -c "from agent.identity import get_identity; print(get_identity().node_id)" 2>/dev/null \
  || echo "unknown"
)

echo ""
echo "✅ Agent deployed successfully!"
echo ""
echo "🆔 Node ID: ${NODE_ID:0:32}..."
echo ""
echo "📊 Status:"
docker exec "${CONTAINER_NAME}" python -m agent.cli status 2>/dev/null | head -20 || true
echo ""
echo "🔗 Endpoints:"
echo "   Local:  http://localhost:${AGENT_PORT}"
echo "   Status: http://localhost:${AGENT_PORT}/status"
echo ""
echo "📋 Commands:"
echo "   View logs:    docker logs -f ${CONTAINER_NAME}"
echo "   Stop agent:   docker stop ${CONTAINER_NAME}"
echo "   Get status:   curl http://localhost:${AGENT_PORT}/status"
echo ""
