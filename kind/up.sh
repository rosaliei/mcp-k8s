#!/usr/bin/env bash
# Create the kind cluster, deploy the broken workloads, and make a READ-ONLY kubeconfig for the MCP server.
# Your normal ~/.kube/config is NOT changed: this cluster gets its own kubeconfig files in this folder.
set -euo pipefail
cd "$(dirname "$0")"
KUBECTL=/usr/local/bin/kubectl            # the real binary (your shell alias points kubectl at kubecolor)
ADMIN=./admin.kubeconfig                  # full access, for you
READER=./mcp-reader.kubeconfig            # read-only, for the MCP server

# Create the cluster only if it doesn't exist yet (so running this script twice is safe).
# --kubeconfig writes the admin login to our own file instead of ~/.kube/config.
if ! kind get clusters | grep -qx mcp-k8s; then
  kind create cluster --config cluster.yaml --kubeconfig "$ADMIN"
fi
kind get kubeconfig --name mcp-k8s > "$ADMIN"

# apply = create or update to match the YAML (safe to run again)
$KUBECTL --kubeconfig "$ADMIN" apply -f rbac-read-only.yaml -f broken-workloads.yaml

# Build a kubeconfig that logs in as the mcp-reader ServiceAccount.
# A kubeconfig has 3 parts: clusters (where: API server URL + its CA certificate),
# users (who: here a ServiceAccount token), and contexts (which user on which cluster).
# `kubectl create token` makes a short-lived token (24h). When it expires: run this script again.
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
chmod 600 "$ADMIN" "$READER"      # only you can read these files: they contain credentials

echo
echo "Cluster ready. Give it ~1 minute for pods to reach their broken states, then:"
echo "  $KUBECTL --kubeconfig kind/admin.kubeconfig get pods -n payments"
echo "  K8S_MODE=kubectl KUBECONFIG=\$PWD/kind/mcp-reader.kubeconfig python mcp_server/client_test.py"
