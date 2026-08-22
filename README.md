# mealie-gkeep-sync

Bidirectional sync between a [Mealie](https://mealie.io) shopping list and a Google Keep
list. Add an item on either side and it appears on the other; tick it off in the aisle and
Mealie agrees. Runs as a single pod next to a self-hosted Mealie.

Only the **food name** crosses to Keep — `1 cup Basil (fresh)` in Mealie shows as `Basil`.
Quantities, units and notes stay in Mealie.

> **Before you start:** Google has no usable official Keep API (no update method,
> Workspace-only), so this uses [`gkeepapi`](https://github.com/kiwiz/gkeepapi) against the
> private Android API. That requires a **master token**, which is password-equivalent —
> full access to the Google account, not just Keep. **Use a dedicated Google account** that
> owns nothing but the shared shopping list.

---

## Prerequisites

- A running Mealie instance and a Kubernetes cluster with a default StorageClass
- `kubectl` and `helm`
- Python 3.12+ on your workstation (one-time, to mint the Google token)

---

## 1. Get a Mealie API token

Mealie → your profile → **Manage Your API Tokens** → *Generate*. Copy it.

## 2. Get a Google master token

Do this once, on your workstation. The password-based login older guides describe now
fails with `BadAuthentication`; use the browser flow below.

**a.** Sign into the browser as the account that owns the Keep list, then open:

```
https://accounts.google.com/EmbeddedSetup
```

An "unsupported browser" page is fine — the cookie is still set. Accept the terms prompt
if one appears.

**b.** Open DevTools → **Application** → **Cookies** → `https://accounts.google.com` and
copy the value of the **`oauth_token`** cookie. It starts with `oauth2_4/`.

**c.** Exchange it for a master token:

```bash
git clone https://github.com/falterfriday/mealie_gkeep_sync
cd mealie_gkeep_sync
python -m venv .venv && . .venv/bin/activate
pip install -e .

python tools/get_master_token.py you@gmail.com
```

Paste the cookie value when prompted. The result starts with `aas_et/`.

The `oauth_token` cookie is single-use and short-lived. If the exchange fails, reopen
EmbeddedSetup for a fresh cookie and retry. The master token stays valid until you change
the account password; revoke it at
[myaccount.google.com/device-activity](https://myaccount.google.com/device-activity).

**Never commit it.** It goes straight into a Secret.

## 3. Create the Secret

Create it out of band so no credential passes through values or your Helm release history:

```bash
kubectl create secret generic mealie-gkeep-sync-creds \
  --from-literal=MEALIE_API_TOKEN='...' \
  --from-literal=GOOGLE_MASTER_TOKEN='aas_et/...'
```

## 4. Install — dry run first

The first sync **unions both lists**. Do a read-only run and read the plan before writing
anything:

```bash
helm repo add mealie-gkeep-sync https://falterfriday.github.io/mealie_gkeep_sync
helm repo update

helm install mealie-gkeep-sync mealie-gkeep-sync/mealie-gkeep-sync \
  --set mealie.baseUrl=http://mealie.mealie.svc.cluster.local:9000 \
  --set mealie.listName=Groceries \
  --set google.email=you@gmail.com \
  --set google.keepListName=Groceries \
  --set secrets.existingSecret=mealie-gkeep-sync-creds \
  --set sync.dryRun=true
```

(Or `helm install mealie-gkeep-sync ./charts/mealie-gkeep-sync` from a clone.)

```bash
kubectl logs -f deploy/mealie-gkeep-sync
```

You will see `would create in Keep`, `would delete from Mealie`, and so on. Nothing is
written.

## 5. Go live

```bash
helm upgrade mealie-gkeep-sync mealie-gkeep-sync/mealie-gkeep-sync --reuse-values \
  --set sync.dryRun=false
```

Past two or three overrides, keep a values file instead:

```bash
helm upgrade --install mealie-gkeep-sync mealie-gkeep-sync/mealie-gkeep-sync -f my-values.yaml
```

## 6. Verify

```bash
kubectl get pods -l app.kubernetes.io/name=mealie-gkeep-sync   # expect 1/1 Running
kubectl logs deploy/mealie-gkeep-sync | tail
```

Add an item in Keep, wait one interval (60s default), and confirm it lands in Mealie.

---

## Configuration

The chart sets these environment variables for you. Full list in
[`charts/mealie-gkeep-sync/values.yaml`](charts/mealie-gkeep-sync/values.yaml).

| Helm value | Env var | Default | Meaning |
|---|---|---|---|
| `mealie.baseUrl` | `MEALIE_BASE_URL` | *required* | Root URL of Mealie |
| `mealie.listName` | `MEALIE_LIST_NAME` | `Groceries` | Target list by name |
| `mealie.listId` | `MEALIE_LIST_ID` | — | Target list by UUID (wins if set) |
| `mealie.verifySsl` | `MEALIE_VERIFY_SSL` | `true` | False only for self-signed certs |
| `google.email` | `GOOGLE_EMAIL` | *required* | Account owning the Keep list |
| `google.keepListName` | `KEEP_LIST_NAME` | `Groceries` | Title of the Keep list |
| `google.createListIfMissing` | `KEEP_CREATE_LIST_IF_MISSING` | `false` | Create it if absent |
| `sync.intervalSeconds` | `SYNC_INTERVAL_SECONDS` | `60` | Poll interval |
| `sync.conflictStrategy` | `CONFLICT_STRATEGY` | `newest` | `newest` \| `mealie` \| `keep` |
| `sync.parseIngredients` | `PARSE_INGREDIENTS` | `true` | Structure Keep text via Mealie's parser |
| `sync.parserMinConfidence` | `PARSER_MIN_CONFIDENCE` | `0.6` | Below this, store as a plain note |
| `sync.createMissingFoods` | `CREATE_MISSING_FOODS` | `false` | Let new text create food records |
| `sync.dryRun` | `DRY_RUN` | `false` | Log the plan, write nothing |
| `logging.level` / `.format` | `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `json` \| `text` |
| `persistence.size` | — | `128Mi` | State volume |

Credentials come from `secrets.existingSecret` (recommended) or `secrets.mealieApiToken` /
`secrets.googleMasterToken`, which land in the release manifest.

The chart refuses to render rather than deploy something broken: missing credentials, no
list selector, an unknown conflict strategy, or `replicaCount > 1`. `values.schema.json`
rejects misspelled keys, so typos fail at install instead of doing nothing. Set
`replicaCount=0` to pause syncing without uninstalling.

---

## How sync behaves

**Merge base.** Each pair records the Mealie ID, Keep ID, and the text and checked state
they last agreed on. That snapshot is what distinguishes "changed here" from "changed
there" from "deleted" — a two-way diff cannot. Text and checked state resolve
independently, so renaming on one side while ticking off on the other is not a conflict.
Genuine conflicts go to `CONFLICT_STRATEGY`.

**Checked items** sync both ways. Checking in Keep does *not* delete from Mealie.

**Deletions** propagate both ways.

**Mealie → Keep** sends only the food name. Free-text items (no food record) have no name,
so their note is used instead.

**Keep → Mealie** parses text through Mealie's own ingredient parser, accepting the result
only when it is confident and matched a food that already exists. Anything else becomes a
plain note, which keeps typos out of your food database. Renaming in Keep preserves the
quantity, unit and note that Keep cannot show — unless you type an amount (`3 tbsp basil`),
which wins.

**State** lives on a small PVC: the ID links plus gkeepapi's node cache. Each Mealie item
also carries its Keep ID in Mealie's `extras`, so losing the volume rebuilds links from
Mealie rather than duplicating everything. The residual risk is that items deleted while
the volume was gone can reappear.

---

## Operating

**Health.** `/healthz` (liveness) stays green while the process runs. `/readyz` goes red
when syncing is actually broken and returns JSON explaining why:

```bash
kubectl exec deploy/mealie-gkeep-sync -- \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/readyz').read().decode())"
```

A rejected master token is deliberately **not** a liveness failure — restarting cannot fix
credentials, and CrashLoopBackOff would hide the logs telling you what to do. The pod stays
up, readiness goes red, the log names the fix.

**Run exactly one replica.** Two syncers race on the same list pair and the same
ReadWriteOnce volume. The Deployment uses `Recreate` so rollouts cannot overlap.

### Troubleshooting

| Symptom | Cause |
|---|---|
| `Authentication failed` in logs | Master token invalid or revoked — redo step 2 |
| `No Mealie shopping list named ...` | Name mismatch; the log lists available lists |
| `No Google Keep list titled ...` | Same, or set `google.createListIfMissing=true` |
| Pod `0/1 Running`, no errors | No successful sync yet — check `/readyz` |
| `ErrImagePull` | GHCR package is private by default; make it public or add a pull secret |

---

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

pytest
ruff check . && mypy src
```

The reconciliation engine (`engine.py`) is pure — data in, an action plan out — so the
whole merge matrix is tested without a network. All I/O lives in `sync.py`.

```
src/mealie_gkeep_sync/
├── config.py       env-driven settings
├── models.py       shared types; Mealie items keep their raw payload
├── mealie.py       thin httpx client (households/groups auto-detected)
├── keep_client.py  gkeepapi wrapper
├── render.py       Mealie item → Keep text, and back
├── engine.py       pure three-way reconciliation
├── sync.py         orchestration: read, plan, apply, persist
├── state.py        atomic JSON state
├── health.py       liveness/readiness
└── __main__.py     loop, backoff, signals
```

CI runs lint, types, tests, a container smoke test under production security constraints,
Helm render/schema checks, and a security suite (gitleaks, pip-audit, Bandit, hadolint,
Trivy, CodeQL). See [`.github/workflows/`](.github/workflows/) and
[`docs/RELEASING.md`](docs/RELEASING.md).

---

## Releasing

Chart and image version independently:

- **Image** — tag `vX.Y.Z` (must equal `appVersion`). Publishes multi-arch amd64/arm64 to
  GHCR, Trivy-scanned *before* push, cosign-signed.
- **Chart** — bump `version:` in `Chart.yaml` on any `charts/**` change, merge to `main`.
  CI fails the PR if you forget, because chart-releaser skips existing versions silently.

Full procedure and one-time setup (GitHub Pages, GHCR visibility) in
[`docs/RELEASING.md`](docs/RELEASING.md).
