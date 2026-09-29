#!/usr/bin/env bash
# Delete ONLY the mcp-k8s kind cluster (your other kind clusters, like gitops-demo, are untouched)
# and the kubeconfig files kind/up.sh created.
set -euo pipefail
cd "$(dirname "$0")"
kind delete cluster --name mcp-k8s
rm -f admin.kubeconfig mcp-reader.kubeconfig
