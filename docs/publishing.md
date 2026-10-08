# Build and publish macjoker/vulcan-notify

Publishing is a maintainer action, separate from deployment. Nothing in this
change publishes an image or updates Docker Hub automatically. The existing
homelab's Git-polling timer is independent of the new workflow.

The same `Dockerfile` supports local source builds and registry images. It includes
Python 3.12, uv, locked dependencies, Playwright Chromium, Xvfb, Openbox, x11vnc
and noVNC. `.dockerignore` excludes credentials, databases, browser state and
Ansible private configuration from build contexts. Runtime state belongs in
`/app/data`, never in the published image.

## Tags

- `macjoker/vulcan-notify:<version>`: version from `pyproject.toml`, e.g. `0.20.0`,
  without a `v` prefix. Treat published version tags as immutable.
- `macjoker/vulcan-notify:latest`: optional alias for an explicitly selected build.
  Publishing a version updates this alias only when requested.
- `macjoker/vulcan-notify@sha256:<digest>`: pin an exact manifest for deployment.

The workflow targets `linux/amd64` and `linux/arm64`. Verify builds and runtime
on both before advertising those platforms as supported. No full image build
or live Docker Hub publication was performed while preparing these files.

## Option A: manual GitHub Actions workflow

[`docker-hub.yml`](../.github/workflows/docker-hub.yml) uses **only
`workflow_dispatch`**. Pushing commits, opening PRs or creating tags does not run
it. It uses [official Docker build actions](https://docs.docker.com/build/ci/github-actions/).

After reviewing and merging the changes yourself:

1. Ensure the workflow is present on the repository default branch, so GitHub
   exposes its **Run workflow** button.
2. Select **Build / publish Docker Hub image (manual)** in GitHub Actions and
   choose the source branch/ref you intend to release.
3. Enter that ref's `pyproject.toml` version (`0.20.0` for this checkout). Leave
   **publish** and **update_latest** unchecked for an initial build check. This
   builds both architectures without Docker Hub login or image push. Build
   cache/summary artifacts may still be uploaded to GitHub.
4. Review logs. ARM64 under QEMU can be slow; the job timeout is 120 minutes.
5. When ready, create Docker Hub repository `macjoker/vulcan-notify` with the
   intended visibility. Generate a token for the `macjoker` account with repository
   write permission and save it as GitHub Actions secret `DOCKERHUB_TOKEN`.
   No application credentials are needed in GitHub.
6. Run again for the same reviewed ref/version with **publish** checked.
   Check **update_latest** only to also move that alias.
7. Verify tags/digest and container startup before deploying hosts.

The workflow validates that the requested version matches the source. It neither
deploys hosts nor changes the Docker Hub description. Do not reuse a released
version for changed code; advance `pyproject.toml` and documented example pins.

## Option B: manual commands from a reviewed checkout

Run these commands when you decide to build/release; they have not been executed
as part of this change. Check the source ref and working tree first:

```bash
git status --short
git rev-parse HEAD
docker buildx create --name vulcan-release --driver docker-container --use
docker buildx inspect --bootstrap
```

Reuse `docker buildx use vulcan-release` if the builder already exists.
Build one native-platform image locally first (no registry upload):

```bash
docker buildx build --load --progress=plain \
  --tag macjoker/vulcan-notify:0.20.0 .
docker run --rm macjoker/vulcan-notify:0.20.0 \
  uv run vulcan-notify --help
```

To check both architectures without publishing:

```bash
docker buildx build --platform linux/amd64,linux/arm64 --progress=plain \
  --output=type=oci,dest=/tmp/vulcan-notify-0.20.0.tar .
```

Use Docker Desktop's emulation or a builder with native AMD64/ARM64 nodes or
configured QEMU. Native ARM64 avoids slow package configuration under emulation;
see [Docker multi-platform guidance](https://docs.docker.com/build/building/multi-platform/).
The Dockerfile already uses `DEBIAN_FRONTEND=noninteractive`. A stall at a package
`Get:` line can indicate download trouble; unpacking/configuration may be slow
under emulation. Inspect full logs to distinguish them.

When you choose to publish, log in interactively with a Docker Hub token:

```bash
docker login --username macjoker
docker buildx build --platform linux/amd64,linux/arm64 --progress=plain \
  --tag macjoker/vulcan-notify:0.20.0 --push .
docker buildx imagetools inspect macjoker/vulcan-notify:0.20.0
```

To also update `latest`, add `--tag macjoker/vulcan-notify:latest` to that build
command before running it. Pushing a single local image would provide only its
one architecture; use the combined build for a multi-platform registry manifest.

## Verify before announcing the image

On representative AMD64/ARM64 hosts, use the
[deployment Compose file](../deploy/docker/compose.yml) with the published tag.
Check API liveness, headed Chromium startup under Xvfb, manual noVNC login and
session/profile persistence across recreation. Real auth needs your account and
should be performed deliberately. Keep one worker owner and inspect `/api/health`
after successful sync. See the [deployment guide](deployment.md).

## Docker Hub description

[`docker-hub.md`](docker-hub.md) is a compact description ready to paste into
Docker Hub **Overview** after publication. It uses absolute repository links for
Compose, `.env` and the Ansible guide so it works outside GitHub. Review and upload
it manually; there is no automatic description upload.
