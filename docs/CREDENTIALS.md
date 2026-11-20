# Credentials

Every form in MOKS that needs a key, token or password takes the value itself: connectors (including the
kubeconfig and the AWS keys for secret sync), LLM providers, GitHub knowledge sources and backup storage. Paste it
and save, or pick one already saved under *… from*. MOKS stores it encrypted and never shows it again.

Settings → Credentials lists what is stored and where each one is used. **Add credential** saves a named value
(an API key used by several providers, say) for any form to pick; **Replace value** changes it everywhere it is
used; a credential nothing uses can be deleted.

Nothing needs a Kubernetes Secret entry any more, apart from what MOKS needs to start:

| Value | Why it stays in the Secret |
|---|---|
| `MOKS_DATABASE_URL` | Stored credentials live in the database, so MOKS must reach it first |
| `MOKS_SESSION_SECRET` | Signs sessions, and by default derives the key that encrypts stored credentials |
| `MOKS_OIDC_CLIENT_SECRET` | Sign-in (oidc mode), read at start-up |

## References

A connector, provider, source or backup store keeps a *reference*, never a value:

| Reference | Value comes from |
|---|---|
| `{"type": "stored", "id": "<uuid>"}` | A value pasted into MOKS (the default in every form) |
| `{"type": "env", "name": "MOKS_CRED_X"}` | An environment variable from a Kubernetes Secret |
| `{"type": "file", "path": "eks/kubeconfig"}` | A file under `MOKS_CREDENTIALS_DIR` |

Environment and file references keep working for seeds, Helm values and API clients, but forms no longer offer
them. A form showing one says so, and a value pasted there replaces it with a stored credential.

## Moving off the Secret

When the API starts, it copies the value of every `MOKS_CRED_*` variable that a connector, LLM provider, knowledge
source or backup store names into stored credentials: one encrypted copy per variable, and those references point
at it. One replica does it (advisory lock), later starts find nothing to do, and the audit log records it as
`credential.import_environment` with `automatic: true`. The Secret itself is not changed. Once **Test all** on
Connectors and each LLM provider's test pass, delete the `MOKS_CRED_*` entries from the Secret. A Secret value
changed after the move is not picked up: replace it in Settings instead.

`MOKS_CREDENTIALS_IMPORT_ON_START=false` turns this off; Settings →
Credentials → **Import from Kubernetes Secret** then does the same on demand.

A reference to a variable the API pod does not have is left alone and listed under *Waiting for a value* with what
uses it. **Paste value** stores it once and points every one of those references at it
(`PUT /admin/credentials/environment/{name}`). Secret sync's AWS keys and a connector's executor credential are the
exception: the executor's own Secret holds those on purpose, so they are never listed as waiting.

`MOKS_CRED_PINECONE_API_KEY` is used by MOKS itself, not by a connector, so it is listed under *Used by the
deployment*. A value set there is used before the environment variable. `MOKS_CRED_RERANKER_TOKEN` stays in
the Secret, because the separate reranker service must hold the same value.

## Encryption

- AES-256-GCM. The key is derived with HKDF-SHA256 from `MOKS_CREDENTIALS_KEY` when it is set, otherwise from
  `MOKS_SESSION_SECRET`.
- Each row stores the id of the key that sealed it. The row id is the associated data, so a ciphertext copied onto
  another row does not open.
- The database, its backups and the API responses hold only ciphertext, labels and who changed what. Values are not
  logged, audited, traced, returned or put in prompts. `tests/test_stored_credentials.py` checks responses, audit
  details and logs for the value.

**Changing the key.** To give stored credentials their own key, or to rotate `MOKS_SESSION_SECRET`:

1. Put the old root value in `MOKS_CREDENTIALS_KEY_PREVIOUS`, which is the current session secret if
   `MOKS_CREDENTIALS_KEY` was never set. Put the new one in `MOKS_CREDENTIALS_KEY` (or the new session secret), and
   roll out.
2. Settings → Credentials → **Re-encrypt** moves every row to the new key.
3. Remove `MOKS_CREDENTIALS_KEY_PREVIOUS` and roll out again.

A row that no available key opens is shown as *unreadable*. Paste its value again.

**Keep your own copy** of the backup encryption key and the volume repository password. Backups, including MOKS's
own database backups, cannot be restored without them, and a copy that lives only inside MOKS's database is lost
with it.

## How a new value reaches every pod

Each process keeps the encrypted rows in memory and checks the table for changes:

- at most every 2 seconds on API requests;
- whenever a worker or the executor claims a job or an investigation;
- every time it reports its build.

A saved value is usable within seconds, and a replaced value rebuilds cached connector clients. Nothing restarts.

## Who may do what

| Action | Permission |
|---|---|
| Store or replace a value from a form; list saved names to pick from | `admin.connectors`, `admin.llm`, `admin.settings` or `knowledge.source_admin` |
| List with usage, add, delete, import, fill a waiting variable, re-encrypt | `admin.settings` |

A credential that something still uses cannot be deleted; the refusal names the users. Every create, replace,
delete, import and re-encryption is in the audit log, with the label and never the value.

## Secret sync

The AWS keys for an `aws_secrets` connector can be stored here like any other credential. When they are, the API
pod can decrypt them too, because it holds the same key. Where that matters, keep using Pod Identity or IRSA (no
keys), or an environment or file reference that only the executor's Secret holds (docs/SECRETS_SYNC.md), set in
the connector's JSON or seed.
