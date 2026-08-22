# Releasing

The chart and the image version **independently**. `version` in `Chart.yaml` tracks the
chart; `appVersion` tracks the application image. The chart's `image.tag` defaults to
`appVersion`, so a normal release is two coordinated edits in one PR — bump `appVersion`
to the new image version, and bump `version` because the chart changed — then tag.

Both halves are enforced in CI:

- **Pull requests** — any change under `charts/` without a `version` increase fails,
  because chart-releaser publishes a version once and then skips it *silently*.
- **Tag pushes** — `vX.Y.Z` must equal `appVersion` exactly, or the image build fails
  before it starts. Otherwise you would publish image `X.Y.Z` while the chart still pulls
  the previous one: an install that looks healthy and quietly runs old code.

**Sequence:** bump both fields in one PR → merge → tag `vX.Y.Z`.

---

## Image releases

`.github/workflows/image-release.yml` publishes to `ghcr.io/<owner>/mealie-gkeep-sync`:

| Trigger | Tags produced |
|---|---|
| push to `main` | `main`, `sha-<short>` — a moving edge build |
| push tag `vX.Y.Z` | `X.Y.Z`, `X.Y`, `latest` |

Built for **linux/amd64 and linux/arm64**, so it runs on an arm64 homelab node as well as
x86. The arm64 image builds under QEMU in roughly two minutes.

The image is **scanned before it is pushed**, not after. A vulnerable image that is already
published cannot be un-published and consumers may have pulled it, so the Trivy gate runs
against a locally loaded amd64 build first; the multi-arch push happens only if it passes,
reusing the same cached layers.

Every push is **signed with cosign** using keyless OIDC — no private key to store or
rotate. Provenance and SBOM attestations are attached. Verify with:

```bash
cosign verify ghcr.io/falterfriday/mealie-gkeep-sync:latest \
  --certificate-identity-regexp='^https://github.com/falterfriday/mealie_gkeep_sync/' \
  --certificate-oidc-issuer=https://token.actions.githubusercontent.com
```

### One-time setup

**The GHCR package starts out private.** After the first publish: package on GitHub →
*Package settings* → visibility → public. Otherwise nodes get `denied` on pull and you need
an `imagePullSecret` plus `imagePullSecrets` in the chart values.

---

## Chart releases

`.github/workflows/chart-release.yml` runs [chart-releaser] on every push to `main`
touching `charts/**`. It packages each chart, creates a GitHub Release, and updates
`index.yaml` on the `gh-pages` branch.

The workflow creates `gh-pages` itself if missing — an empty root commit pushed via git
plumbing, so nothing is checked out and packaged charts survive. Without it chart-releaser
fails with `fatal: invalid reference: origin/gh-pages`.

### One-time setup

**Settings → Pages → Deploy from a branch → `gh-pages` / root.** Creating the branch is
enough for chart-releaser to publish; enabling Pages is what makes the Helm repo URL serve
over HTTPS.

GitHub Pages on a private repository requires a paid plan — on the free tier the repo must
be public for the Helm repo URL to work. If that does not suit, publish the chart as an OCI
artifact to GHCR (`helm push` to `oci://ghcr.io/...`), which works for private repos and
needs no `gh-pages` branch.

[chart-releaser]: https://github.com/helm/chart-releaser-action

---

## Notes on the image vulnerability gate

The image scan uses `ignore-unfixed`. Debian ships CVEs with no available fix at any given
moment — currently 4 CRITICAL and 13 HIGH in the base — and gating on those would block
every build with no action available. They are still reported by the informational sweep
and uploaded to the Security tab; the weekly scheduled run is what surfaces them once fixes
land. A distroless or Alpine base would cut most of that residual surface, at the cost of a
harder debugging story.

To keep the *fixable* count at zero, the image does two things beyond the norm:

1. Applies Debian security updates on top of the base, because the upstream Python image
   lags Debian security releases.
2. Deletes `pip` and `setuptools` from both the system and the venv after install. The app
   never installs anything at runtime, and those two packages were the image's entire
   remaining Python CVE surface.
