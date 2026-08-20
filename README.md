# mealie-gkeep-sync

Bidirectional sync between a [Mealie](https://mealie.io) shopping list and a Google Keep
list. Add an item on either side and it shows up on the other; tick it off on your phone
mid-aisle and Mealie agrees.

Built to run as a single pod next to a self-hosted Mealie instance.

---

## How it talks to Google Keep

Google's **official** Keep API cannot do this. It exposes only `create`, `get`, `list`
and `delete` on notes — there is no update or patch method, so you cannot toggle a
checkbox or edit an item without deleting and recreating the whole note. It is also
restricted to Google Workspace service accounts with domain-wide delegation.

So this app uses [`gkeepapi`](https://github.com/kiwiz/gkeepapi), a mature unofficial
client for the private Android Keep API. Worth knowing about the risk profile:

- **The sync protocol is stable.** The node model and `changes` endpoint have been
  essentially unchanged for years.
- **Authentication is the churny part** — and it is a *one-time, manual* step. The
  password-based login older guides describe now fails with `BadAuthentication`; the
  browser-assisted flow in `tools/get_master_token.py` replaces it. The master-token →
  OAuth exchange the pod runs hourly has been stable throughout.

If Google ever does break the protocol layer, the fix is `pip install -U gkeepapi`
rather than a rewrite here.

### Security: the master token is password-equivalent

`GOOGLE_MASTER_TOKEN` grants **full access to the Google account** — not just Keep. It is
not scoped and not revocable per-app except by revoking the device at
[myaccount.google.com/device-activity](https://myaccount.google.com/device-activity).

**Strongly consider a dedicated Google account** that owns nothing but the shared
shopping list, rather than pointing this at your primary account. Keep the token in a
Kubernetes Secret (or your secrets manager), never in Git.

---

## Sync semantics

### The merge base

Every synced pair is recorded as a link — the Mealie item ID, the Keep item ID, and the
text and checked value they **last agreed on**. That snapshot is what makes this a real
three-way merge: comparing each side against the base is the only way to distinguish
"changed here" from "changed there" from "deleted", which a two-way diff cannot do.

| Mealie | Keep | Outcome |
|---|---|---|
| unchanged | unchanged | nothing |
| changed | unchanged | push Mealie → Keep |
| unchanged | changed | push Keep → Mealie |
| changed | changed, same value | no writes, base moves |
| changed | changed, different | conflict → `CONFLICT_STRATEGY` |
| present | missing | deleted in Keep → delete in Mealie |
| missing | present | deleted in Mealie → delete in Keep |

Text and checked state resolve **independently**, so renaming an item on one side while
ticking it off on the other is not a conflict.

### Checked items

Checking an item syncs the checked state both ways. Items stay on both lists until
explicitly deleted — checking something in Keep does **not** delete it from Mealie.

### Structured items ↔ plain text

Mealie items are structured (quantity / unit / food / note); Keep items are one line of
text.

- **Mealie → Keep** renders a readable line, preferring Mealie's own `display` field so
  the Keep list reads exactly like the Mealie UI.
- **Keep → Mealie** runs the text through Mealie's own `/api/parser/ingredients`, so
  items typed on your phone land structured with real food/unit/quantity records where
  possible.

A parse is only accepted when it is confident, matched a food that **already exists** in
Mealie, and either found no unit or matched a real one. Everything else becomes a plain
note item. That last condition matters: accepting a parse whose unit did not resolve
would silently drop the unit word — and since Mealie is the canonical renderer, that loss
would be written back over your text in Keep.

**Mealie is the canonical renderer.** When Keep text parses into a structured item, the
way Mealie renders it back may differ from what you typed (`2lb chicken` →
`2 lb Chicken breast`). That canonical text is pushed back to Keep in the same cycle, so
both sides converge immediately rather than ping-ponging.

Set `PARSE_INGREDIENTS=false` to import everything from Keep as plain notes instead.

### State and recovery

Two files live on the PVC:

- `sync-state.json` — the links and merge base.
- `keep-state.json` — gkeepapi's node cache, which doubles as the Keep sync cursor.

Both are written atomically. Each Mealie item also carries its Keep ID in Mealie's
`extras` field, so if the volume is lost the links **rebuild from Mealie alone** rather
than duplicating every item on both sides. The remaining risk of losing the volume is
that items deleted while it was gone can reappear.

---

## Setup

### 1. Mealie API token

Mealie → user profile → *Manage Your API Tokens* → Generate.

### 2. Google master token

```bash
pip install -e .
python tools/get_master_token.py you@gmail.com
```

Follow the prompts (the script explains where to find the `oauth_token` cookie). The
result starts with `aas_et/`.

### 3. Deploy the Helm chart

The chart lives at `charts/mealie-gkeep-sync`. Create the Secret out of band so no
credential passes through values or your release history:

```bash
kubectl create secret generic mealie-gkeep-sync-creds \
  --from-literal=MEALIE_API_TOKEN='...' \
  --from-literal=GOOGLE_MASTER_TOKEN='aas_et/...'
```

**Do a dry run first** — it prints exactly what it would do to each side without
writing anything. Worth it whenever either list already has items, because the first
sync unions them:

```bash
helm install mealie-gkeep-sync charts/mealie-gkeep-sync \
  --set mealie.baseUrl=http://mealie.mealie.svc.cluster.local:9000 \
  --set mealie.listName=Groceries \
  --set google.email=you@gmail.com \
  --set google.keepListName=Groceries \
  --set secrets.existingSecret=mealie-gkeep-sync-creds \
  --set sync.dryRun=true
```

Read the logs, then `helm upgrade` with `--set sync.dryRun=false` to go live. For
anything beyond a couple of overrides, keep a values file instead:

```bash
helm upgrade --install mealie-gkeep-sync charts/mealie-gkeep-sync -f my-values.yaml
```

The chart refuses to render rather than deploying something that cannot work: missing
credentials or account identity, no list selector, an unknown conflict strategy, or a
`replicaCount` above 1 (concurrent syncers race on the same list pair and the same
ReadWriteOnce volume). `values.schema.json` additionally rejects misspelled keys, so a
typo fails at install instead of silently doing nothing. Set `replicaCount=0` to pause
syncing without uninstalling.

See `charts/mealie-gkeep-sync/values.yaml` for every option; the table below maps them
to the environment variables the app actually reads.

### Installing from the published Helm repository

Once the chart has been released (see below), it can be installed without cloning:

```bash
helm repo add mealie-gkeep-sync https://falterfriday.github.io/mealie_gkeep_sync
helm repo update
helm install mealie-gkeep-sync mealie-gkeep-sync/mealie-gkeep-sync -f my-values.yaml
```

### Publishing a chart release

`.github/workflows/chart-release.yml` runs [chart-releaser] on every push to `main`
that touches `charts/**`. It packages each chart, creates a GitHub Release for it, and
updates `index.yaml` on the `gh-pages` branch.

The workflow creates the `gh-pages` branch itself if it is missing — as an empty root
commit pushed via git plumbing, so nothing is checked out and the packaged charts
survive. Without it, chart-releaser fails with `fatal: invalid reference:
origin/gh-pages`.

One step remains manual: **Settings → Pages → Deploy from a branch → `gh-pages` /
root.** Creating the branch is enough for chart-releaser to publish; enabling Pages is
what makes the Helm repo URL actually serve over HTTPS.

GitHub Pages on a private repository requires a paid plan; on the free tier the
repository must be public for the Helm repo URL to work. If that does not suit,
publishing the chart as an OCI artifact to GHCR (`helm push` to `oci://ghcr.io/...`)
works for private repositories and needs no `gh-pages` branch at all.

**Bump `version:` in `Chart.yaml` for every chart change.** chart-releaser publishes a
given version once and skips it thereafter, so an unbumped change merges cleanly and
publishes nothing, silently. CI enforces this on pull requests: any diff under
`charts/` without a version increase fails the build. `version` tracks the chart;
`appVersion` tracks the application image and moves independently.

[chart-releaser]: https://github.com/helm/chart-releaser-action

### Publishing the image

`.github/workflows/image-release.yml` publishes to
`ghcr.io/<owner>/mealie-gkeep-sync`:

| Trigger | Tags produced |
|---|---|
| push to `main` | `main`, `sha-<short>` — a moving edge build |
| push tag `vX.Y.Z` | `X.Y.Z`, `X.Y`, `latest` |

Built for **linux/amd64 and linux/arm64**, so it runs on an arm64 homelab node as well
as x86. Verified locally: the arm64 image builds under QEMU in roughly two minutes and
starts correctly.

The image is **scanned before it is pushed**, not after. A vulnerable image that is
already published cannot be un-published, and consumers may have pulled it, so the
Trivy gate runs against a locally loaded amd64 build first and the push only happens
if it passes. The multi-arch push reuses the same cached layers.

Each published image is **signed with cosign** using keyless OIDC, so there is no
private key to store or rotate. Verify one with:

```bash
cosign verify ghcr.io/falterfriday/mealie-gkeep-sync:latest \
  --certificate-identity-regexp='^https://github.com/falterfriday/mealie_gkeep_sync/' \
  --certificate-oidc-issuer=https://token.actions.githubusercontent.com
```

Provenance and SBOM attestations are attached to every push.

**The GHCR package starts out private.** After the first publish, go to the package on
GitHub → *Package settings* → change visibility to public, otherwise cluster nodes get
`denied` on pull and you will need an `imagePullSecret` and
`image.pullSecrets` in the chart values.

#### Keeping chart and image versions in step

They are deliberately separate: `version` in `Chart.yaml` is the chart, `appVersion` is
the application. The chart's `image.tag` defaults to `appVersion`, so a release is
normally two coordinated edits — bump `appVersion` to the new image version and bump
`version` because the chart changed. Tag the repo `vX.Y.Z` to publish that image
version.

Both halves are enforced:

- **Pull requests**: any change under `charts/` without a `version` increase fails CI,
  because chart-releaser publishes a version once and then skips it silently.
- **Tag pushes**: `vX.Y.Z` must match `appVersion` exactly, or the image build fails
  before it starts. Otherwise you would publish image `X.Y.Z` while the chart still
  pulls the previous version — an install that looks fine and quietly runs old code.

So the release sequence is: bump both fields in one PR, merge it, then tag.

---

## Configuration

Environment variables are what the app reads; the chart sets them for you.

| Variable | Helm value | Default | Meaning |
|---|---|---|---|
| `MEALIE_BASE_URL` | `mealie.baseUrl` | *required* | Root URL of the Mealie instance |
| `MEALIE_API_TOKEN` | `secrets.mealieApiToken` | *required* | Mealie API token |
| `MEALIE_LIST_ID` | `mealie.listId` | — | Target list UUID (takes precedence) |
| `MEALIE_LIST_NAME` | `mealie.listName` | — | Target list name; one of ID/name is required |
| `MEALIE_VERIFY_SSL` | `mealie.verifySsl` | `true` | Set false only for self-signed internal certs |
| `MEALIE_TIMEOUT_SECONDS` | `mealie.timeoutSeconds` | `30` | HTTP timeout |
| `GOOGLE_EMAIL` | `google.email` | *required* | Account owning the Keep list |
| `GOOGLE_MASTER_TOKEN` | `secrets.googleMasterToken` | *required* | From `tools/get_master_token.py` |
| `KEEP_LIST_NAME` | `google.keepListName` | *required* | Title of the Keep list |
| `KEEP_CREATE_LIST_IF_MISSING` | `google.createListIfMissing` | `false` | Create the list if absent |
| `SYNC_INTERVAL_SECONDS` | `sync.intervalSeconds` | `60` | Poll interval (both sides require polling) |
| `CONFLICT_STRATEGY` | `sync.conflictStrategy` | `newest` | `newest` \| `mealie` \| `keep` |
| `PARSE_INGREDIENTS` | `sync.parseIngredients` | `true` | Structure Keep text via Mealie's parser |
| `PARSER_MIN_CONFIDENCE` | `sync.parserMinConfidence` | `0.6` | Below this, fall back to a plain note |
| `CREATE_MISSING_FOODS` | `sync.createMissingFoods` | `false` | Let unrecognised text create food records |
| `DRY_RUN` | `sync.dryRun` | `false` | Log the plan, write nothing |
| `STATE_DIR` | *fixed at /data* | `/data` | Where state files live |
| `HEALTH_PORT` | `health.port` | `8080` | Health endpoint port |
| `LOG_LEVEL` | `logging.level` | `INFO` | |
| `LOG_FORMAT` | `logging.format` | `json` | `json` \| `text` |

`CONFLICT_STRATEGY=newest` compares modification timestamps and falls back to Mealie when
either side does not report one.

---

## Operating it

`/healthz` (liveness) stays green while the process is alive. `/readyz` (readiness) goes
red when syncing is actually broken — bad credentials, or no successful sync for three
intervals — and returns JSON explaining why:

```bash
kubectl exec deploy/mealie-gkeep-sync -- \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/readyz').read().decode())"
```

A rejected master token is deliberately **not** a liveness failure. Restarting cannot fix
credentials, and a CrashLoopBackOff would hide the logs that explain the problem. The pod
stays up, readiness goes red, and the log says exactly what to do.

Run exactly one replica. The Deployment uses `Recreate` so a rollout cannot briefly run
two syncers against the same list.

---

## CI and security scanning

Five workflows in `.github/workflows/`:

**`ci.yml`** — ruff, `mypy --strict` and pytest across Python 3.12 (the floor in
`pyproject.toml`) and 3.13 (what the image ships), then builds the image and smoke tests
it under the same security constraints as the Deployment: read-only root filesystem,
tmpfs `/tmp`, all capabilities dropped. The smoke test runs with `--network none` so CI
never makes outbound auth attempts to Google; the resulting connection failure is the
point, because it asserts the process stays up, `/healthz` stays 200, `/readyz` returns
503, and SIGTERM still exits 0.

It also lints and renders the Helm chart, validates the output against real Kubernetes
API schemas with kubeconform, asserts the rendered Deployment still carries every
operational invariant (single replica, `Recreate`, read-only root filesystem, probe
paths, config checksum), and round-trips the chart's guard rails: six unsafe value
combinations must be rejected and five supported ones must render.

**`security.yml`** — on every PR and weekly on a schedule:

| Scan | Tool | Gate |
|---|---|---|
| Secrets | gitleaks (full history) | any finding fails |
| Dependency CVEs | pip-audit | any finding fails |
| Python SAST | Bandit (medium+) | any finding fails |
| Dockerfile lint | hadolint | warning+ fails |
| Manifest misconfig | Trivy config, on the Helm-rendered output | HIGH/CRITICAL fails |
| Image CVEs | Trivy image | fixable HIGH/CRITICAL fails |
| SBOM | Syft (SPDX) | artifact |

Secret scanning runs first and deliberately gates the rest: this repo's worst-case
failure is a leaked `GOOGLE_MASTER_TOKEN`, which is account-wide access rather than a
scoped key.

**`codeql.yml`** — GitHub's own SAST with `security-extended`.

**`chart-release.yml`** — packages and publishes `charts/**` to the `gh-pages` Helm
repository after a change lands on `main`. See *Publishing a chart release* above.

**`image-release.yml`** — builds and publishes the container image to GHCR. See
*Publishing the image* below.

Findings gate the build via **exit codes**, so the pipeline works on public and private
repos alike. SARIF upload to the Security tab is best-effort (`continue-on-error`)
because it needs Advanced Security on private repos.

### Notes on the image gate

The image scan uses `ignore-unfixed`. Debian ships CVEs with no available fix at any
given moment — currently 4 CRITICAL and 13 HIGH in the base — and gating on those would
block every build with no action available. Those are still reported by the
informational sweep and uploaded to the Security tab. The weekly schedule is what
surfaces them once fixes land. If that residual surface matters to you, a distroless or
Alpine base would cut most of it, at the cost of a harder debugging story.

To keep the *fixable* count at zero the image does two things beyond the norm: it applies
Debian security updates on top of the base (the upstream Python image lags Debian
security releases), and it deletes `pip` and `setuptools` from both the system and the
venv after install. The app never installs anything at runtime, and those two packages
were the image's entire remaining Python CVE surface.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

pytest              # reconciliation matrix, rendering, state, clients, logging
ruff check . && mypy src
```

The reconciliation engine (`engine.py`) is pure — plain data in, an action plan out — so
the entire merge matrix is tested without touching a network. All I/O lives in `sync.py`.

```
src/mealie_gkeep_sync/
├── config.py       env-driven settings
├── models.py       shared types; Mealie items keep their raw payload
├── mealie.py       thin httpx client (households/groups auto-detected)
├── keep_client.py  gkeepapi wrapper
├── render.py       structured item ↔ text line
├── engine.py       pure three-way reconciliation
├── sync.py         orchestration: read, plan, apply, persist
├── state.py        atomic JSON state
├── health.py       liveness/readiness
└── __main__.py     loop, backoff, signals
```
