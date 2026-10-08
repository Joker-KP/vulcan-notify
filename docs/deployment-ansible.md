# Deploy the Docker Hub image with Ansible

[`deploy/ansible/deploy.yml`](../deploy/ansible/deploy.yml) is an example for a Linux
Docker host reachable over SSH. It uses the same
[image-based Compose file](../deploy/docker/compose.yml) as the
[default deployment guide](deployment.md).

The playbook creates application/data directories, installs Compose and `.env`,
pulls your selected image and starts only `vulcan-api` and `vulcan-sync`.
`vulcan-auth` remains available under the explicit `auth` profile. Existing database,
session and Chromium files are preserved. No source checkout, host application
dependencies, image build, publishing, update timer or live authentication is performed.

Publish an image before deployment: see [publishing](publishing.md). `0.20.0` is an
example matching this source version, not a claim of existing Docker Hub availability.

## Prerequisites

- Controller/workstation: Python 3.11+, Ansible Core 2.19 and the pinned
  `community.docker` collection in `requirements.yml`.
- Target: Linux AMD64/ARM64, Python 3, running Docker Engine, and Compose plugin
  2.18+. Install Docker using [its official guide](https://docs.docker.com/engine/install/).
- SSH access and sudo rights. Add `--ask-become-pass` if sudo needs a password.
  These instructions assume the standard rootful Docker socket.
- Target can pull from Docker Hub and reach eduVULCAN/configured integrations.
  Public images need no registry login; for a private repository, first run
  `sudo docker login` on the host with a read-only token.

Host provisioning stays with your infrastructure setup; the playbook checks
Docker/Compose instead of replacing packages. See the
[official Compose module reference](https://docs.ansible.com/ansible/latest/collections/community/docker/docker_compose_v2_module.html)
for requirements and convergence behavior.

## 1. Prepare controller files

From a repository checkout:

```bash
python3 -m venv /tmp/vulcan-ansible
. /tmp/vulcan-ansible/bin/activate
python -m pip install 'ansible-core>=2.19,<2.20'
ansible-galaxy collection install -r deploy/ansible/requirements.yml

mkdir -p deploy/ansible/private
cp deploy/ansible/inventory.example.yml deploy/ansible/private/inventory.yml
cp deploy/ansible/vars.example.yml deploy/ansible/private/vars.yml
cp .env.example deploy/ansible/private/vulcan.env
chmod 0700 deploy/ansible/private
chmod 0600 deploy/ansible/private/*
```

`deploy/ansible/private/` is excluded from Git and Docker build contexts. Edit:

- `private/inventory.yml`: Docker host address, SSH user and target Python path.
- `private/vars.yml`: installation directory, published image version/digest and
  API bind address. The example pins `macjoker/vulcan-notify:0.20.0`.
- `private/vulcan.env`: application settings. Optional credentials enable automatic
  recovery; otherwise use noVNC login after deployment. SMTP/MQTT/AI examples are
  in the [canonical `.env.example`](../.env.example).

Keep `vulcan_env_file` absolute or use `{{ playbook_dir }}/private/vulcan.env` as
shown in the vars example; it is a controller path, not a target path. Target `.env`
incorporates that file and appends `VULCAN_IMAGE` and `VULCAN_API_BIND` from Ansible.
Set these deployment options in `vars.yml`; application options stay in `vulcan.env`.

The API defaults to loopback. For Home Assistant on another trusted machine, set
`vulcan_api_bind` to the Docker host's LAN IPv4 address and limit access to trusted
clients. Ports are fixed at 8585 for API and loopback 6080 for temporary noVNC.
The API has no built-in authentication.

Application/data directories are root-owned, mode 0700; target `.env` is root-owned,
mode 0600. Use sudo for operational commands.

## 2. Optional Ansible Vault

Encrypt the entire environment file on the controller if desired:

```bash
ansible-vault encrypt deploy/ansible/private/vulcan.env
ansible-vault edit deploy/ansible/private/vulcan.env
```

Add `--ask-vault-pass` (or your `--vault-id`) to playbook commands below. The template
lookup reads encrypted files through Ansible's data loader. Docker needs a decrypted
`.env` on the target, stored with mode 0600. Rendering and Compose convergence use
`no_log`, and configuration diffs are suppressed. If convergence fails, inspect
`sudo docker compose logs` on the host for diagnostics.

Do not pass credentials in command-line extra variables. Single-quote literal
secrets containing `$`, `#` or spaces following
[Compose's environment-file syntax](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/#env-file-syntax).

## 3. Review, then deploy when ready

Syntax validation does not contact the host:

```bash
ansible-playbook -i deploy/ansible/private/inventory.yml \
  deploy/ansible/deploy.yml \
  -e @deploy/ansible/private/vars.yml --syntax-check
```

For a host-side preview, add `--check --diff` instead of `--syntax-check`.
Prerequisite checks still run read-only Docker commands. File/directory changes
are predicted; container convergence is skipped, so check mode does not validate
image availability or startup. Secret diffs remain suppressed.

When you choose to deploy the reviewed configuration and image:

```bash
ansible-playbook -i deploy/ansible/private/inventory.yml \
  deploy/ansible/deploy.yml \
  -e @deploy/ansible/private/vars.yml
```

The playbook waits up to 120 seconds for running containers and API liveness.
This does not prove authenticated synchronization or school-data freshness.
An installation without a valid session may need manual login; quiet hours may
delay the first sync.

## 4. Authenticate and verify

On the target:

```bash
sudo -s
cd /opt/vulcan-notify  # or your configured vulcan_install_dir
docker compose stop vulcan-sync
docker compose --profile auth up vulcan-auth
```

From the workstation:

```bash
ssh -N -L 6080:127.0.0.1:6080 deploy@docker-host
```

Open <http://127.0.0.1:6080/vnc.html> and complete login. After successful auth exit,
on the target:

```bash
docker compose --profile auth stop vulcan-auth
docker compose up -d vulcan-sync
docker compose ps
docker compose logs --tail 50 vulcan-sync
curl --fail http://127.0.0.1:8585/api/alive
curl -s http://127.0.0.1:8585/api/health
```

Use your configured LAN address instead of loopback if you changed the API bind.
`/api/health` returns 503 for stale/missing data until a successful sync; Docker
healthchecks use `/api/alive`. Auth and one-off syncs must not overlap the worker.

## Updates and configuration changes

Edit `private/vulcan.env` or `vulcan_image` in `private/vars.yml` and rerun the same
playbook. Compose pulls the selected image and recreates services when their image
or resolved configuration changes. Unchanged configuration/image should leave
services running. Registry availability is checked on each run; use immutable
version tags or a digest for controlled upgrades.

Edit controller files, not generated target `.env`; the next run replaces it.
Normal target commands read the saved image/bind without Ansible or shell exports.
Stop manual auth before rerunning the playbook, because deployment starts the worker.

Follow [backup, rollback and migration instructions](deployment.md#5-backup-update-and-rollback)
before upgrades or replacing an existing installation. Disable the old source-build
auto-deploy timer before switching ownership to this playbook.
