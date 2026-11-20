#!/usr/bin/env bash
# MOKS end to end on a real API server: a kind cluster, the chart as it ships (deploy/helm/moks/values-kind.yaml),
# one lab incident investigated and one Deployment restarted by the executor through an action plan.
# CI's e2e job runs this on every pull request. Never point it at a shared cluster: KUBECONFIG must be kind's.
#   docker buildx build --load -t moks:e2e . && IMAGE=moks:e2e scripts/e2e-kind.sh
set -euo pipefail
: "${IMAGE:?set IMAGE to a locally built MOKS image, as repository:tag}"
context="$(kubectl config current-context)"
[[ "$context" == kind-* ]] || { echo "refusing: the current context is not a kind cluster" >&2; exit 1; }
here="$(cd "$(dirname "$0")/.." && pwd)"
NS=moks-sre
LAB=moks-sre-lab
PORT="${MOKS_E2E_PORT:-18080}"
EMAIL=e2e@moks.test
forward=""

step() { printf '\n== %s\n' "$*"; }

diagnose() {  # on a failure, what a person would look at first
  set +e
  step "diagnostics"
  kubectl get pods -A -o wide
  kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -n 40
  for pod in $(kubectl -n "$NS" get pods -o name); do
    kubectl -n "$NS" describe "$pod" | sed -n '/^Init Containers:/,$p' | head -n 80
    kubectl -n "$NS" logs "$pod" --all-containers --tail=120 --prefix
  done
  kubectl -n "$LAB" get deployments,pods -o wide
}
finish() {
  code=$?
  [[ -z "$forward" ]] || kill "$forward" 2>/dev/null || true
  [[ $code -eq 0 ]] || diagnose
  exit "$code"
}
trap finish EXIT

step "load $IMAGE into the cluster"
kind load docker-image "$IMAGE" --name "${KIND_CLUSTER:-kind}"

# The lab goes first. The crash loop then has restarted by the time MOKS looks, and a previous container's logs
# exist only after a restart.
step "lab: a crash-looping Deployment (payments) and a healthy one to restart (moks-e2e)"
kubectl apply -f "$here/lab/00-namespace.yaml"
kubectl apply -f "$here/lab/02-crashloop-config.yaml" -f "$here/lab/e2e-phase3.yaml"

step "install the chart"
kubectl -n "$NS" get secret moks-secrets >/dev/null 2>&1 || "$here/scripts/create-secrets.sh" "$context"
api_ip="$(kubectl -n default get endpointslices -l kubernetes.io/service-name=kubernetes -o jsonpath='{.items[0].endpoints[0].addresses[0]}')"
service_ip="$(kubectl -n default get service kubernetes -o jsonpath='{.spec.clusterIP}')"
helm upgrade --install moks "$here/deploy/helm/moks" -n "$NS" -f "$here/deploy/helm/moks/values-kind.yaml" \
  --set image.repository="${IMAGE%:*}" --set image.tag="${IMAGE##*:}" \
  --set-json "networkPolicy.apiServer={\"cidrs\":[\"$service_ip/32\",\"$api_ip/32\"],\"ports\":[443,6443]}"
kubectl -n "$NS" rollout status statefulset/moks-postgres --timeout=300s
# Each pod migrates in an init container, which fails and is retried until PostgreSQL answers.
for workload in moks-api moks-worker moks-executor; do
  kubectl -n "$NS" rollout status "deployment/$workload" --timeout=420s
done
kubectl -n "$LAB" rollout status deployment/moks-e2e --timeout=180s
revision_before="$(kubectl -n "$LAB" get deployment moks-e2e -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"

step "one administrator, the documented way"
password="$(openssl rand -hex 16)"
printf '%s\n' "$password" | kubectl -n "$NS" exec -i deployment/moks-api -c api -- moks create-user "$EMAIL" --password-stdin --grant admin

step "investigate, then restart through an action plan"
kubectl -n "$NS" port-forward service/moks-api "$PORT:8080" >/dev/null 2>&1 &
forward=$!
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/moks/api/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/moks/api/health"; echo
MOKS_E2E_URL="http://127.0.0.1:$PORT/moks" MOKS_E2E_EMAIL="$EMAIL" MOKS_E2E_PASSWORD="$password" python3 "$here/scripts/e2e_kind.py"

step "what the cluster says, independently of MOKS"
revision_after="$(kubectl -n "$LAB" get deployment moks-e2e -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
restarted_at="$(kubectl -n "$LAB" get deployment moks-e2e -o jsonpath='{.spec.template.metadata.annotations.kubectl\.kubernetes\.io/restartedAt}')"
echo "moks-e2e: revision $revision_before -> $revision_after, restartedAt ${restarted_at:-<none>}"
[[ "$revision_after" -eq $((revision_before + 1)) && -n "$restarted_at" ]] || { echo "FAIL  the Deployment was not restarted exactly once" >&2; exit 1; }
kubectl -n "$LAB" rollout status deployment/moks-e2e --timeout=60s

step "the audit chain of everything above verifies"
kubectl -n "$NS" exec deployment/moks-api -c api -- moks verify-audit

echo
echo "MOKS end to end on kind: passed"
