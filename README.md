# DataOps Guardian

Python automation for health checks and safe maintenance of data platforms,
starting with Elasticsearch. It runs on  Kubernetes , deploys through Helm, and
rolls back automatically when an upgrade fails.

## What works today

- **Health checks and preflight validation** with retries and  error handling
- **Elasticsearch version discovery** with a live integration test
- **Maintenance planning** as a dry run with an approval gate; it does not
  execute restarts or upgrades
- **Kubernetes deployment** through a Helm chart with probes, resource limits,
  zero-downtime rollouts and a non-root, read-only container
- **Audited deployments** through `scripts/deploy.sh`, which validates the chart,
  installs or upgrades, rolls back on failure and logs each step
- **CI** that lints, tests, builds images and runs Elasticsearch integration
  tests and a Helm install on  a kind cluster

- **Elasticsearch upgrade readiness checks** that decide whether a cluster is
  safe to upgrade, using read-only API calls

- **Rolling Elasticsearch upgrades on Kubernetes**, one node at a time, with
  resume after failure
- **Splunk reporting** that sends every check and upgrade report to the HTTP
  Event Collector

Planned: Kibana dashboards, and upgrade automation for Splunk.

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
on the cluster. For a secured cluster, set `ELASTICSEARCH_API_KEY`, or
`ELASTICSEARCH_USERNAME` and `ELASTICSEARCH_PASSWORD`.

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

## Run a rolling Elasticsearch upgrade

`k8s/elasticsearch/elasticsearch.yaml` deploys a three-node test cluster with
security disabled. Without `--execute`, the command only prints a plan:

```bash
dataops-guardian-es-upgrade --target-version 8.19.12
```

```bash
dataops-guardian-es-upgrade --target-version 8.19.12 --execute --accept-warnings
```

The tool runs the readiness checks, then for each node, with the elected master
last:

1. Restricts shard allocation to primaries and flushes
2. Deletes the pod so it restarts on the new image
3. Waits for the node to rejoin on the target version
4. Restores allocation and waits for shards to recover

It finishes by checking that the cluster is green, every node runs the target
version, allocation is restored and no documents were lost.

### Why there is no automatic rollback

Elasticsearch nodes cannot be downgraded after they start on a newer version, so
rolling the image back would not work. The tool stops at the first failure,
restores shard allocation and leaves remaining nodes untouched. Fix the cause and
continue with `--resume`, which may use a different target but never an older one.
Recovering from a bad upgrade means restoring a snapshot, which is why the
readiness checks look for a recent one.

### End-to-end test

`scripts/e2e_es_upgrade.sh` runs in GitHub Actions because it needs more memory
than a typical laptop can spare. It upgrades 8.17.4 to 8.19.12 to 9.5.5 while a
probe inside the cluster reads and writes every second, and it covers a blocked
upgrade path, a dry run and a bad image followed by a resume. The test fails if
any acknowledged write or seeded document is lost.

## Send reports to Splunk

```bash
export SPLUNK_HEC_URL=https://splunk.example:8088
export SPLUNK_HEC_TOKEN=your-token
```

Both commands then send their reports with the sourcetype `dataguardian:report`.
A Splunk outage is recorded in the report and never changes the upgrade's outcome.
`compose.splunk.yaml` starts a local Splunk for testing; starting it accepts
Splunk's license and terms.

## Project layout

| Path | Purpose |
|---|---|
| `src/dataguardian/` | Application code |
| `tests/` | Unit and integration tests |
| `helm/dataops-guardian/` | Helm chart |
| `scripts/` | Deployment and audit logging |
| `k8s/elasticsearch/` | Three-node Elasticsearch test cluster |
| `.github/workflows/tests.yml` | CI pipeline |
| `.github/workflows/platform-e2e.yml` | Live upgrade and Splunk tests |
