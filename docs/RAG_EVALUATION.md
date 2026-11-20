# Retrieval evaluation

How MOKS measures knowledge retrieval, and what the SRE knowledge engine (document types, structure-aware
chunking, named-service ranking, query signals and context assembly) changed, measured on the same questions
before and after. Retrieval only: no model answers here unless a run is in `llm` mode.

## Datasets

| Dataset | Version | What it checks |
|---|---|---|
| `rag` | (by hash) | safety and governance: access isolation per binding, the evaluation realm, abstention, freshness and supersession, conflicts, injection through retrieved text, exact identifiers, claim support, citation offsets, reranker fallback |
| `sre` | 1 | retrieval quality over an operations corpus: 25 documents (runbooks, postmortems, alert rules, a `PrometheusRule`, manifests, Helm values, Terraform, two Grafana dashboards, an OpenAPI description, a README, a dependency table, a log) and 28 questions, among them every example the knowledge engine was asked to answer: why is payment-service failing, what changed in the last hour, find similar OOMKilled incidents, how do I troubleshoot RabbitMQ backlog, which services depend on Redis, find the runbook for EKS node pressure |

Both live in `backend/moks/evals/datasets/`, run in the isolated evaluation realm (docs/KNOWLEDGE.md, Evaluation
isolation) against real PostgreSQL ranking, and run in CI (`tests/test_eval_gates.py`): every gate must pass, and on
`sre` the current pipeline must not score below the baseline on recall@5, MRR, nDCG@5, precision@3, context precision
or context recall. Change a dataset's questions or answers and raise its `version`; a run records the version and the
dataset's hash, the pipeline versions, the fusion weights and the switches it ran with.

Run one from **Settings > Evaluation**, or `moks eval sre` (add `--mode llm` for answer faithfulness) in a worker pod.

## Metrics

Ranking metrics are per document (several chunks of one document count once), at k = 5 results.

| Metric | Meaning |
|---|---|
| Recall@k | share of a question's relevant documents in the first k |
| Precision@k | share of the first k that are relevant (divided by k, so a question with one relevant document scores at most 0.2 at k = 5) |
| MRR | 1 / rank of the first relevant document |
| nDCG@5 | rank-discounted gain against the ideal order |
| Context precision | of the blocks the assembled context holds, are the relevant ones first: mean precision at each relevant block |
| Context recall | share of the question's `facts` (phrases a correct answer needs) present in the assembled context; without facts, relevant documents present |
| Citation accuracy | share of returned passages whose offsets and lines hold exactly their text in the stored document |
| Context tokens | estimated tokens of the assembled context (4 characters a token) |
| Answer faithfulness | `llm` mode only: the active chat model answers from the assembled context, citing passages; the share of its sentences whose cited passages contain their words (`claim_support`, literal overlap, not entailment). Not a gate |
| Latency | end-to-end search, p50 and p95, in the evaluation realm |

**Baseline** is search as it was before the knowledge engine: splitting by file format only, no query signals and no
named-service ranking, and context laid out in rank order, each passage clipped. **Current** is the deployment's
configuration. Both index the same corpus separately, answer the same questions under the same grants, and are
compared on the questions both can run (the baseline had no filters, so the two filter questions are left out of the
comparison).

## Results, 2026-11-20

`sre` version 1, `rules` mode, five runs on an arm64 Mac (Python 3.12, PostgreSQL 17.11), the evaluation realm
(keyword and exact-identifier ranking; no vectors). With ties broken by document path and position (below), the five
runs gave identical quality figures; only latency varied. Raw results: [examples/sre-rag-evaluation-2026-11-20.json](examples/sre-rag-evaluation-2026-11-20.json).

**Like for like: the 24 questions both ran**

| | Baseline | Current | Change |
|---|---:|---:|---:|
| Recall@1 | 0.660 | 0.681 | +0.021 |
| Recall@3 | 0.875 | 0.965 | +0.090 |
| Recall@5 | 0.875 | 0.979 | +0.104 |
| Precision@1 | 0.833 | 0.875 | +0.042 |
| Precision@3 | 0.417 | 0.486 | +0.069 |
| Precision@5 | 0.250 | 0.300 | +0.050 |
| MRR | 0.882 | 0.938 | +0.056 |
| nDCG@5 | 0.878 | 0.936 | +0.058 |
| Context precision | 0.861 | 0.922 | +0.061 |
| Context recall | 0.833 | 1.000 | +0.167 |
| Context tokens (mean) | 520 | 467 | -10% |
| Citation accuracy | 1.000 | 1.000 | |
| Search latency p50 / p95 (mean of runs) | 3.2 / 4.6 ms | 4.0 / 5.6 ms | +0.7 / +0.9 ms |

**All 28 questions, current:** recall@5 0.981, precision@5 0.300, MRR 0.942, nDCG@5 0.941, context precision 0.928,
context recall 1.000, citation accuracy 1.000. Every gate passed: isolation, realm isolation, exact identifiers,
abstention, freshness, filters, declared dependencies, claim support, citation accuracy, reranker fallback, recall@5.
The `rag` dataset's gates all still pass.

**Where it changed**

| Question | Baseline | Current | Why |
|---|---|---|---|
| What changed for payments in the last hour? | neither changed document in the top 5 | both first | the time window and the recent signal |
| Why is payment-service failing with 5xx errors? | 2 of 3 relevant; facts missing from context | 3 of 3; all facts | `payment-service` resolves to `payments`, whose documents are searched by name |
| Which services depend on Redis? | the dependency table only | table and both manifests (`REDIS_HOST`); `dependents: redis → cart, payments` | named-service ranking; dependents from `applies_to.dependency_of` |
| Which queue does the orders service consume from? | README only | README and the consumer manifest | named-service ranking |
| What threshold does the RabbitMQ backlog alert use? | dashboard first | the alert rule first | alert intent prefers alert rules |
| Has the cart service been OOMKilled before? / Find similar OOMKilled incidents | the postmortem, without its root cause | with its root cause | past-incident questions bring the postmortem's root cause and summary |
| payments pod OOMKilled exit code 137, what should I check? | runbook third | runbook second, its steps in context | runbook preference; adjacent chunks joined |
| Is Redis persistence enabled in the Helm values? | values file first | **values file second** (MRR 1.0 to 0.5) | named-service ranking lifts the Redis runbook, whose text says "redis"; the values file never does |

**Still missed:** "orders messages ending up in the dead letter queue" does not reach the RabbitMQ runbook, which says
"dead letter queue orders.dlq" but none of "ending" or "orders messages". A purely lexical miss: vector search, absent
here, is what closes this kind of gap.

## How the values were set, and what to read into this

Document types, the chunkers, the signal sizes (document type 0.006, section 0.003, recent 0.006) and the
named-service weight (0.5) were set before the first run. The first run then led to four changes, all in this
release, so the figures above are not independent of the dataset:

1. Section preference applies only to past-incident questions. For runbooks it ranked a "Mitigation" section above
   a runbook titled with the question ("How do I roll back a deployment?").
2. Named-service ranking was added after manifests could not be found for "which services depend on Redis" and
   "which queue does the orders service consume from": a manifest never says "depend" or "service".
3. Past-incident questions bring a postmortem's root cause and summary into context, after two questions found the
   postmortem but not the section that answers them.
4. Ties are broken by document path and position instead of a random chunk id. Before this, MRR and context
   precision moved by up to 0.04 between identical runs.

Read this as evidence of direction, not of production quality. The corpus is small and synthetic, and the same
person wrote it and the feature. There are no vectors in the evaluation realm, so production's hybrid ranking will
differ: vectors may close misses like the dead-letter one, and may shrink what named-service ranking adds. The
reranker was not part of either variant. Latency was measured on a laptop against a tiny index; the added cost is one
catalog query and one more keyword ranking per search. Answer faithfulness needs an `llm`-mode run with a chat model,
and none was made for these figures.

Next: build a dataset from real operator questions (Knowledge > Retrieval test feedback is the place to collect
them), run `moks eval sre --mode llm` on mk4 for faithfulness, and compare with vectors on.

**The vector store is unchanged.** PostgreSQL with pgvector stays. Nothing measured here favours another store: what
moved the numbers is what is indexed and how it is ranked and laid out, which any store would need. The embedding
cache cuts embedding cost the same way in front of PostgreSQL or Pinecone.
