# Offline (air-gapped) deployment

The target host has no internet access. Everything must be transferred on
removable media and verified before use.

## 1. What the bundle contains

| Item | Why |
|------|-----|
| `patchtrack-3.0.0.tar.gz` | Application source |
| `patchtrack-app-3.0.0.tar` | The built service image (`docker save`) |
| `ubi9-python-312.tar` | Base image, if not already present on the target |
| `postgres-17.tar` | PostgreSQL image |
| `grafana-oss-13.0.2.tar` | Optional dashboard image |
| `patchtrack-wheels/` | Python wheels for native installation |
| `SHA256SUMS` | Digest of every file above |

Do not transfer an image by copying a container's filesystem; use
`docker save` so the image's metadata and layer digests survive.

## 2. Build the bundle on a connected host

Use a host with the **same architecture** (x86_64). Prefer the same OS family
so the base images match what the target can run.

```bash
set -euo pipefail
OUT=/srv/bundles/patchtrack-3.0.0
mkdir -p "$OUT/patchtrack-wheels"
cd /opt/PatchTrack

# Application image
docker build -f deploy/container/Dockerfile -t patchtrack:3.0.0 .
docker save patchtrack:3.0.0 -o "$OUT/patchtrack-app-3.0.0.tar"

# Dependency images
docker pull registry.access.redhat.com/ubi9/python-312:1-1762230134
docker save registry.access.redhat.com/ubi9/python-312:1-1762230134 -o "$OUT/ubi9-python-312.tar"

docker pull docker.io/library/postgres:17
docker save docker.io/library/postgres:17 -o "$OUT/postgres-17.tar"

docker pull docker.io/grafana/grafana-oss:13.0.2
docker save docker.io/grafana/grafana-oss:13.0.2 -o "$OUT/grafana-oss-13.0.2.tar"

# Source + wheels for a native fallback
# Builds a verified archive that contains no .env, keys or runtime state.
scripts/build-release.sh 3.0.0
cp dist/patchtrack-3.0.0.tar.gz dist/patchtrack-3.0.0.tar.gz.sha256 "$OUT/"
pip download -r deploy/container/requirements.txt \
  --dest "$OUT/patchtrack-wheels" \
  --only-binary=:all: --python-version 3.12 --platform manylinux_2_17_x86_64

cd "$OUT"
sha256sum ./*.tar ./*.tar.gz ./patchtrack-wheels/* > SHA256SUMS
sha256sum -c SHA256SUMS
```

`--only-binary=:all:` matters: a wheel that has to be compiled needs a
compiler and the PostgreSQL headers on the target, which an air-gapped host
usually lacks.

## 3. Transfer and verify on the target

Carry the bundle on approved media, then:

```bash
cd /srv/bundles/patchtrack-3.0.0
sha256sum -c SHA256SUMS
```

Every line must read `OK`. Do not proceed on a mismatch; re-transfer.

> Treat the bundle as untrusted input until the digests verify. Extract it into
> its own new empty directory and do not run anything from inside it before
> that check.

## 4. Load the images

```bash
docker load -i patchtrack-app-3.0.0.tar
docker load -i ubi9-python-312.tar
docker load -i postgres-17.tar
docker load -i grafana-oss-13.0.2.tar
docker images | grep -E 'patchtrack|postgres|grafana'
```

## 5. Configure compose to use the local images

Compose will not pull if the image is present, but it may still try to resolve
the base image names. Two safeguards in `.env`:

```bash
PATCHTRACK_VERSION=3.0.0
POSTGRES_IMAGE=docker.io/library/postgres:17
```

and, if the host cannot reach any registry, run compose with pulls disabled:

```bash
docker compose up -d --no-build --pull never
```

If you have pushed the images into a local registry on the target, tag them to
the registry name and use those names instead. That is the cleaner long-term
arrangement when more than one host is involved.

## 6. Operating system packages

The host needs no internet for the container deployment. If you install
Docker/Podman from a local repository, point `dnf` at it:

```bash
sudo dnf --disablerepo='*' --enablerepo='local-rhel9-baseos' install -y \
  docker-ce docker-compose-plugin
```

For the **native** (non-container) deployment you also need the runtime
libraries the wheels expect — at minimum `python3.12`, `libpq`, `openssl-libs`
and the HarfBuzz/Freetype libraries ReportLab uses:

```bash
sudo dnf --disablerepo='*' --enablerepo='local-rhel9-appstream' install -y \
  python3.12 python3.12-pip postgresql13-libs openssl-libs \
  freetype harfbuzz
```

## 7. Install from the bundle

Extract the source archive into a new empty directory and run the guided
installer with the **bundle directory**, not the extracted source directory:

```bash
sudo ./install.sh --airgap --bundle-dir /srv/bundles/patchtrack-3.0.0
sudo ./install.sh --airgap --bundle-dir /srv/bundles/patchtrack-3.0.0 --check
```

The installer verifies `SHA256SUMS` itself, so the manual checks in steps 3 and
4 are not required. It checks that the application and PostgreSQL archives are
present, loads the images that are not already on the host, skips the image
build when the application image is supplied, does not add a remote Docker CE
repository, and starts Compose with pulls disabled. See
[Air-gapped installation](install.md#air-gapped-installation) for the full
list of changes.

Interactive mode asks the same question; enter the bundle path when prompted.
The bundle directory is recorded in `install.json` so the uninstaller and the
operator know which bundle the installation came from.

## 8. Updates on an air-gapped host

1. Build the replacement bundle at the new version, on a connected host.
2. Verify digests on the target.
3. Load the image, then follow
   [upgrade-rollback.md](upgrade-rollback.md).

Keep the previous version's image on the target until the new one has been
verified in production. Rollback is an image tag change plus a database
restore, and both are faster with the old image still present.

## 9. What does not need to be in the bundle

- CA certificates: use the enterprise CA distribution, not the bundle.
- Configuration values: these are generated on the target.
- Data: never ship a database dump with a bundle; it will contain user rows and
  audit history.
