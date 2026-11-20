kubectl -n moks rollout restart deploy moks-api moks-worker moks-executor moks-terminal moks-crewai
kubectl -n moks rollout status deploy moks-worker
