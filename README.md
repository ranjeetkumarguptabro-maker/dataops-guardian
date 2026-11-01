# DataOps Guardian

Python automation for health checks and safe maintenance of data platforms,
starting with Elasticsearch. It runs on Kubernetes, deploys through Helm, and
rolls back automatically when an upgrade fails.

## What works today

- **Health checks and preflight validation** with retries and error handling
- **Elasticsearch version discovery** with a live integration test
- **Maintenance planning** as a dry run with an approval gate; it does not
  execute restarts or upgrades
- **Kubernetes deployment** through a Helm chart with probes, resource limits,
  zero-downtime rollouts and a non-root, read-only container
- **Audited deployments** through `scripts/deploy.sh`, which validates the chart,
  installs or upgrades, rolls back on failure and logs each step
- **CI** that lints, tests, builds images and runs Elasticsearch integration
  tests and a Helm install on a kind cluster

- **Elasticsearch upgrade readiness checks** that decide whether a cluster is
  safe to upgrade, using read-only API calls

Planned: running Elasticsearch upgrades on Kubernetes, then Splunk integration.

## Run the tests

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

## Deploy to Minikube

```bash
minikube start --driver=docker
docker build --target production -t dataops-guardian:local .
minikube image load --overwrite=true dataops-guardian:local
./scripts/deploy.sh
```

Check the service:

```bash
kubectl port-forward service/dataops-guardian 18080:80
```

```bash
curl http://localhost:18080/health
```

The chart reuses the `local` tag, so after loading a rebuilt image restart the
pods to pick it up:

```bash
kubectl rollout restart deployment/dataops-guardian
```

Use the Helm chart for this release. The `k8s/` manifests are a plain reference
and lack the chart's security settings. Creating the Deployment with `kubectl`
first also gives `kubectl` ownership of its fields, which makes later Helm
upgrades fail with a conflict.

## Failure handling

`scripts/deploy.sh` reads `CHART` and `TIMEOUT` from the environment, which makes
a failed upgrade easy to rehearse. Point `CHART` at a copy of the chart whose
image tag does not exist:

```bash
CHART=/path/to/broken-chart TIMEOUT=40s ./scripts/deploy.sh
```

The script exits with status 1 and restores the previous revision. Old pods keep
serving throughout, because the rollout uses `maxUnavailable: 0` and new pods
never become ready. Each step is recorded in `logs/maintenance.log`.

## Check Elasticsearch upgrade readiness

```bash
dataops-guardian-upgrade-check --url http://localhost:9200 --target-version 8.19.0
```

The command prints a JSON report, saves a copy in `reports/` and changes nothing
on the cluster. For a secured cluster, set `ELASTICSEARCH_USERNAME` and
`ELASTICSEARCH_PASSWORD`.

| Check | Blocks the upgrade when | Warns when |
|---|---|---|
| `upgrade_path` | The target is a downgrade or skips a required version | |
| `cluster_health` | Red, or yellow with several nodes | Yellow on a single node |
| `shards` | Shards are relocating or initializing | |
| `shard_allocation` | Allocation is not set to `all` | |
| `nodes` | Versions are mixed, or heap use is 90% or more | Heap use is 75% or more, or there is only one node |
| `disk` | Disk use is 85% or more | Disk use is 75% or more |
| `deprecations` | Any critical deprecation exists | Any deprecation warning exists |
| `snapshot` | | No successful snapshot in the last 24 hours |

The report status is `ready`, `ready_with_warnings` or `blocked`. The command
exits with status 1 only when blocked, so it can gate a CI job or deployment
script. A check that cannot get a valid answer counts as failed, never as passed.

Upgrade paths follow
[Elastic's documentation](https://www.elastic.co/docs/deploy-manage/upgrade/prepare-to-upgrade):
9.1 and later require 8.19, 9.0 requires 8.18 or 8.19, and 8.x requires 7.17.

## Project layout

| Path | Purpose |
|---|---|
| `src/dataguardian/` | Application code |
| `tests/` | Unit and integration tests |
| `helm/dataops-guardian/` | Helm chart |
| `scripts/` | Deployment and audit logging |
| `.github/workflows/tests.yml` | CI pipeline |
