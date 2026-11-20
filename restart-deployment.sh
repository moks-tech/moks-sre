kubectl -n moks-sre rollout restart deploy moks-api moks-worker moks-executor moks-terminal moks-crewai
kubectl -n moks-sre rollout status deploy moks-worker
