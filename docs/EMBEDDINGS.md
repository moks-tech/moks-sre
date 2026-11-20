# Embeddings and semantic retrieval

How MOKS turns text into vectors, how a model is chosen and replaced, and what the SRE benchmark measured.
Search itself (keyword, exact identifiers and vectors fused by RRF, then access checks) is in [KNOWLEDGE.md](KNOWLEDGE.md#retrieval).

**Decision (2026-11-20):** replace bge-m3 with **BAAI/bge-base-en-v1.5 with its query instruction**, served by the
new `moks-embedder` service. On 85 SRE questions it is statistically indistinguishable from bge-m3 in both hybrid and
vector-only ranking, at under a third of the CPU per chunk and of the query latency, and less memory (0.8 to 0.9 GB
against bge-m3's 1.28 GiB on mk4). bge-m3 keeps serving until the new model passes the in-cluster evaluation and an index is rebuilt with it.
[Rollout](#rollout-on-mk4) has the steps.

## One interface for every provider

Everything that embeds goes through `moks/rag/embeddings.py`: indexing, search, the dimension probe, the connection
test, rebuilds and evaluations. An embedding configuration (Settings > LLM providers, purpose `embedding`) names a
provider and a model. The `Embedder` adds what a provider does not know:

| | |
|---|---|
| Query and document prefixes | Some models are trained with an instruction before the query or `query: ` / `passage: ` prefixes. Set per configuration; nothing is assumed from the model's name. |
| Truncation | `max_input_chars` cuts inputs explicitly; otherwise the model's own limit applies. |
| Unit vectors | Every vector is L2-normalised. All backends rank by cosine, which this does not change, so older vectors still compare. Zero, empty or non-finite vectors are refused. |
| Shape checks | One request returns one vector per input, all the same size; a configured size must match the real one. |
| Metering | Every call goes through the LLM budget, like chat. |

| Provider | What it talks to | Notes |
|---|---|---|
| `huggingface` | A Text Embeddings Inference API: `POST /embed`, `GET /info` | MOKS's own `moks-embedder`, a TEI server or a Hugging Face Inference Endpoint. The connection test checks that `/info` reports the configured model and revision. Embedding only. |
| `sentence_transformers` | A model loaded in the MOKS process | For development, single-node installs and the benchmark. The MOKS image does not include it; a cluster uses the embedding service. Remote code never runs. |
| `ollama`, `openai` (and OpenAI-compatible), `azure_openai`, `gemini`, `bedrock` | As before | Unchanged, now behind the same interface. |

Nothing names a model or a size in code. `moks/rag/embedding_profiles.yaml` lists suggested prefixes for known models
(and whether this benchmark measured them); the form fills them in for a new configuration and never changes a saved one.

### The model key

An index records the model key it was built with: `provider/model/dimensions`, plus a fingerprint of the options that
change a vector (prefixes, truncation, revision) once any is set. A configuration without options keeps the key it
always had, so existing indexes stay valid. A configuration whose key no longer matches its index writes no vectors
into it; the API refuses such an edit to a configuration an index still uses (add a new configuration instead).

## Index generations

A generation is one physical index built with one model (`embedding_indexes`). It records the model key, the vector
size, `spec` (provider, model, revision, prefixes, fingerprint, and the evaluation that allowed it), and how many
vectors it reused. One generation per backend is active; retired ones are kept for `MOKS_RAG_RETIRED_RETENTION_HOURS`
(168 by default) so they can be activated again.

- **One size per generation, enforced by PostgreSQL.** pgvector writes cast to `vector(N)` with the generation's size,
  so a vector of another size is refused by the database, whatever the caller checked. Queries only ever read one
  generation's rows.
- **Content hashing and reuse.** Each stored vector carries `input_hash`, a SHA-256 of the model key and the exact
  model input. A rebuild with the same model copies vectors instead of calling the model; editing one section of a
  document re-embeds that section only (the old vectors are read before the rewrite deletes them). On mk4's bge-m3 at
  ~0.7 chunks/s, that is the difference between minutes and seconds for an unchanged corpus.
- **Incremental by default.** Changes go through the outbox as before; documents indexed while embedding was down
  are backfilled.

### Replacing a model

```text
add a configuration for the new model (inactive)
  → Settings > Evaluations: dataset "embedding", that model   (gates below)
  → Knowledge > Rebuild index…: choose that model              (refused until the evaluation passed)
      the new generation is built beside the serving one; search keeps answering with the OLD model,
      because a query is embedded by the model of the generation it searches
  → the switch, in one transaction: new generation active, its model the active embedding model
  → rollback: Index generations > Activate again on the retired generation; its model becomes active with it
```

There is no keyword-only window: activating a new embedding configuration early no longer breaks vector search, it
only marks what the next rebuild uses. Deactivating every embedding model is still the switch that stops vector
search. A rollback is refused if the old generation's configuration was deleted or changed since.

**The evaluation gate** (`MOKS_RAG_REQUIRE_EMBEDDING_EVALUATION`, on; editable in Settings > Limits & schedules)
applies only to a *replacement*: a model whose key differs from an active generation that has vectors. A first index,
a rebuild with the same model, and a keyword-only index have nothing to lose and are never gated. The newest finished
`embedding` evaluation of that exact model key must have passed.

## The evaluation

`backend/moks/evals/datasets/embedding.yaml`: 81 synthetic documents and 85 questions across Kubernetes, AWS,
Terraform, Prometheus and Grafana, Loki and LogQL, OpenTelemetry, incident reports, runbooks and Python or
infrastructure code. Each question grades its documents 3 (answers it), 2 (clearly relevant) or 1 (background), lists
hard negatives (same words, wrong answer), and has a category and a question type. It includes the three examples
from the brief (why is my pod restarting; Prometheus shows high API latency; Terraform deployment failed).

Measures (`moks/evals/retrieval_metrics.py`): Recall@1/3/5/10, Precision@5, MRR@10, graded nDCG@10, MAP, answer
first (a grade-3 document at rank 1), and semantic separation: mean cosine to answers minus to hard negatives, and the
AUC of relevant over irrelevant pairs. Each is reported overall, by category and by question type, for keyword-only,
vector-only and hybrid ranking. The hybrid ranking is search's own: the production identifier extraction, the exact
identifier ranking counted twice, RRF with k=60 and a pool of 32. The keyword ranking is BM25 with a light stemmer,
an approximation of PostgreSQL's `ts_rank_cd`.

**In the cluster** (Settings > Evaluations, dataset `embedding`): the chosen model and the model serving search embed
the dataset through the production path and their budgets; nothing is indexed. Gates:

| Gate | Passes when |
|---|---|
| `vector_adds_value` | hybrid nDCG@10 is above keyword-only |
| `not_worse_than_serving` | hybrid nDCG@10 and Recall@5 within `MOKS_RAG_EMBEDDING_EVAL_TOLERANCE` (0.02) of the serving model's; not applicable when nothing serves vectors |
| `exact_identifiers` | no identifier question loses its exact answer from the top three that keyword search had there |
| `consistent_vectors` | one size, unit length, finite |
| `query_latency` | p95 for one query within the 2 s search waits for an embedding |

**Offline** (`python -m moks.evals.embedding_bench candidates.yaml --out DIR`): any number of candidates, each in its
own process, through the same providers. Adds load time, cold and warm query latency, throughput at production chunk
length (1,300 characters), CPU seconds per chunk, peak memory, bytes per vector, and a paired bootstrap of each
candidate against keyword-only and against a reference model. `docs/benchmarks/embeddings-2026-11-20/` has the
candidates, `results.json` and `report.md` of the run below.

## Results

Measured 2026-11-20 on an arm64 laptop (Apple M3 Pro, 4 torch threads per model, fp32). Quality does not depend on
the machine; speed and memory do. Full tables: [report.md](benchmarks/embeddings-2026-11-20/report.md).

| Model | Dims | nDCG@10 hybrid | nDCG@10 vector | Recall@5 hybrid | Answer first | Query p50 | Chunks/s | CPU s/chunk | Bytes/vector |
|---|---|---|---|---|---|---|---|---|---|
| keyword only (no model) | - | 0.885 | - | 0.859 | 0.776 | - | - | - | - |
| all-MiniLM-L6-v2 | 384 | 0.921 | 0.890 | 0.893 | 0.835 | 3 ms | 120 | 0.016 | 1,544 |
| bge-small-en-v1.5 + instruction | 384 | 0.921 | 0.896 | 0.875 | 0.835 | 6 ms | 33 | 0.069 | 1,544 |
| **bge-base-en-v1.5 + instruction** | 768 | 0.920 | 0.915 | 0.885 | 0.847 | 16 ms | 12.7 | 0.133 | 3,080 |
| e5-base-v2 + prefixes | 768 | 0.919 | 0.902 | 0.880 | 0.824 | 16 ms | 12.6 | 0.135 | 3,080 |
| bge-m3 (serving today) | 1024 | 0.923 | 0.927 | 0.879 | 0.835 | 54 ms | 3.6 | 0.451 | 4,104 |

Is a difference real? Paired bootstrap over the 85 questions, nDCG@10, 95% interval:

| Model | hybrid vs keyword only | hybrid vs bge-m3 | vector vs bge-m3 |
|---|---|---|---|
| all-MiniLM-L6-v2 | +0.036 [+0.015, +0.059] | -0.002 [-0.017, +0.013] | **-0.037 [-0.071, -0.005]** |
| bge-small-en-v1.5 + instruction | +0.036 [+0.017, +0.059] | -0.001 [-0.011, +0.010] | -0.031 [-0.068, +0.002] |
| **bge-base-en-v1.5 + instruction** | +0.035 [+0.014, +0.059] | -0.003 [-0.016, +0.011] | -0.013 [-0.043, +0.017] |
| bge-base-en-v1.5, no instruction | +0.035 [+0.014, +0.058] | -0.003 [-0.015, +0.010] | **-0.041 [-0.079, -0.004]** |
| e5-base-v2 + prefixes | +0.034 [+0.017, +0.054] | -0.004 [-0.022, +0.016] | -0.025 [-0.056, +0.004] |

What this says:

- **Vectors earn their place.** Every model lifts hybrid nDCG@10 by about 0.035 over keyword-only search, and the
  interval excludes zero. The gain is in troubleshooting questions (0.81 to about 0.88) and Kubernetes (0.81 to
  about 0.90), where people describe symptoms in their own words; identifier questions were already good.
- **Bigger is not better in hybrid search.** In the mode search actually runs, no candidate differs measurably from
  bge-m3. Exact identifiers and keywords already carry what the larger model adds.
- **Vector-only quality separates them.** That is what answers a question whose words match nothing, and it matters
  more as the corpus grows. Only bge-base with its instruction stays indistinguishable from bge-m3; MiniLM and both
  bge models without their instruction are measurably worse.
- **Prefixes are model-specific, so they are measured, not assumed.** bge-base's query instruction adds +0.028
  vector nDCG@10 (interval [+0.012, +0.047]); bge-small's +0.014 is not significant; e5's prefixes changed nothing
  measurable here, despite its model card.
- **all-MiniLM-L6-v2 reads 256 tokens.** MOKS chunks run to about 350 tokens, so it would embed the start of most
  production chunks only. This dataset's documents are shorter (median 487 characters), so the benchmark flatters it.

### Cost on mk4

mk4 runs three arm64 nodes with 4 vCPU and 8 GiB each, and memory requests were at 82, 89 and 65% (2026-11-19).
bge-m3 runs there in Ollama: 1.28 GiB, ~650 ms per warm query and ~0.7 chunks/s measured on the cluster.

The embedding service at the chart's settings (batch 8), measured on the laptop:

| | threads | peak memory | chunks/s | query |
|---|---|---|---|---|
| bge-base-en-v1.5 | 2 | 0.79 GB | 12.9 | 17 ms |
| bge-base-en-v1.5 | 4 | 0.87 GB | 17.4 | 19 ms |
| bge-small-en-v1.5 | 2 | 0.54 GB | 34.4 | 6 ms |

On the laptop bge-base embeds 3.5 times as fast as bge-m3 and answers a query 3.3 times as fast, so on mk4 expect
roughly 2 to 3 chunks/s and well under 650 ms per query. That is an extrapolation across runtimes (Ollama there,
torch here); the in-cluster evaluation measures the real latency through the real path before anything switches.

### Why bge-base-en-v1.5, and when to pick something else

bge-base with its query instruction is the smallest model with no measurable quality loss against bge-m3 in either
mode, and it fits mk4 with room to spare: less memory than the pod it replaces, under a third of the CPU per chunk, a
quarter less vector storage, English-only as MOKS's sources are, 512 tokens per input, MIT licence, arm64 CPU.

- **bge-small-en-v1.5 + instruction** if memory or rebuild time is the constraint: 0.55 GB and 2.6 times bge-base's
  throughput, at a vector-only loss that is borderline (interval touches zero). Same hybrid quality.
- **Keep bge-m3** for non-English sources or chunks far beyond 512 tokens; bge-m3 reads 8,192.
- Not all-MiniLM-L6-v2 (256-token input) and not e5-base-v2 (no gain over bge-base, weakest separation margin).

### Limits of this evidence

- The dataset is synthetic and small (85 questions), written by one author, who also wrote the expansion list.
  Treat differences under ~0.02 as noise, as the intervals do.
- Its documents are shorter than production chunks, which favours short-context models.
- Keyword ranking is BM25, not PostgreSQL's. The in-cluster evaluation uses the same approximation, but the
  production identifier logic and fusion.
- bge-m3 ran in torch here and in Ollama (F16 GGUF of the same weights) on mk4: the same model, not the same numbers.

## Query understanding

`moks/rag/query.py`, without a model, beside the question and never instead of it:

- **Normalization**: compatibility forms, invisible characters, typographic quotes and dashes, whitespace. Case,
  punctuation inside names and every identifier stay as typed; tests check `OOMKilled`, `aws_subnet.private[0]`,
  `{app="api"} |= "timeout"` and ARNs survive unchanged.
- **Entities**, by type: Kubernetes objects (`pod/api-7f9c…`), statuses (`CrashLoopBackOff`), namespaces, pod names,
  AWS ARNs, resource ids, regions and services, S3 URIs, Terraform addresses and modules, metric names, PromQL
  functions, label selectors, LogQL stages, OpenTelemetry attributes, trace ids, HTTP statuses, exit codes, environment
  variables, exceptions, file paths, IPs, versions, services and technologies. Whole ARNs, S3 URIs and indexed
  Terraform addresses, which the identifier pattern splits, are added to the exact-match ranking; services named as
  `app="checkout-api"` feed the existing service signal.
- **Question type**: identifier, troubleshooting, how-to, conceptual, incident history, code or general, with the
  rule that decided it. Agrees with the dataset's labels 74% of the time. It is reported in `understanding` and does
  not change ranking: at that agreement, ranking by type would treat about one question in four as the wrong kind.
- **Expansion** (`MOKS_RAG_QUERY_EXPANSION`, **off**): `query_expansions.yaml` maps phrases to related terms
  ("restarting": CrashLoopBackOff, OOMKilled…) that form one more keyword ranking. Measured +0.011 nDCG@10 on
  keyword-only search and +0.008 to +0.014 on hybrid, but the same person wrote the list and the dataset, so that
  gain is optimistic. Turn it on after measuring it on real questions.

## Rollout on mk4

1. Merge. CI builds `moks-embedder:latest` (the reranker image with `embedder.lock.json`, every file
   hash-checked, smoke-tested with a real `/embed`). Argo CD creates the Deployment in the `moks-reranker` app; it
   pulls until CI has pushed the image, then starts. Nothing uses it yet.
2. Restart the API and worker Deployments so they run the new code (images are `:latest`, [RELEASES.md](RELEASES.md)):
   migration 0040 adds the columns, and the seed adds the inactive configuration `bge-base-en-v1-5`.
3. Settings > LLM providers > `bge-base-en-v1-5` > Test: checks the service runs `BAAI/bge-base-en-v1.5` at
   `a5beb1e3…`.
4. Settings > Evaluations > Run evaluation: dataset `embedding`, model `bge-base-en-v1-5`.
5. If it passed: Knowledge > Rebuild index… > `bge-base-en-v1-5`. bge-m3 answers searches until the switch.
6. If anything is wrong after the switch: Index generations > Activate again on the bge-m3 generation.
7. After the retention window, the bge-m3 Ollama release (kubernetes repo, namespace `embeddings`) can go; until
   then it is the rollback.
