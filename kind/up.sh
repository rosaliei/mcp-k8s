#!/usr/bin/env bash
# Create the kind cluster, deploy the broken workloads, and make a READ-ONLY kubeconfig for the MCP server.
# Your normal ~/.kube/config is NOT changed: this cluster gets its own kubeconfig files in this folder.
set -euo pipefail
cd "$(dirname "$0")"
KUBECTL=/usr/local/bin/kubectl            # the real binary (your shell alias points kubectl at kubecolor)
ADMIN=./admin.kubeconfig                  # full access, for you
READER=./mcp-reader.kubeconfig            # read-only, for the MCP server

if ! kind get clusters | grep -qx mcp-k8s; then
  kind create cluster --config cluster.yaml --kubeconfig "$ADMIN"
fi
kind get kubeconfig --name mcp-k8s > "$ADMIN"

$KUBECTL --kubeconfig "$ADMIN" apply -f rbac-read-only.yaml -f broken-workloads.yaml

# Build a kubeconfig that logs in as the mcp-reader ServiceAccount (token valid 24h)
TOKEN=$($KUBECTL --kubeconfig "$ADMIN" -n mcp-system create token mcp-reader --duration=24h)
SERVER=$($KUBECTL --kubeconfig "$ADMIN" config view --raw -o jsonpath='{.clusters[0].cluster.server}')
CA=$($KUBECTL --kubeconfig "$ADMIN" config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
cat > "$READER" <<KCFG
apiVersion: v1
kind: Config
clusters:
- name: mcp-k8s
  cluster: {server: "$SERVER", certificate-authority-data: "$CA"}
users:
- name: mcp-reader
  user: {token: "$TOKEN"}
contexts:
- name: mcp-reader@mcp-k8s
  context: {cluster: mcp-k8s, user: mcp-reader}
current-context: mcp-reader@mcp-k8s
KCFG
chmod 600 "$ADMIN" "$READER"

echo
echo "Cluster ready. Give it ~1 minute for pods to reach their broken states, then:"
echo "  $KUBECTL --kubeconfig kind/admin.kubeconfig get pods -n payments"
echo "  K8S_MODE=kubectl KUBECONFIG=\$PWD/kind/mcp-reader.kubeconfig python mcp_server/client_test.py"
