# WebhookWise Kubernetes Manifests

These manifests provide a production-shaped baseline for running WebhookWise on Kubernetes:

- API deployment with `/live` and `/ready` probes
- horizontally scalable TaskIQ worker deployment
- single-replica scheduler deployment
- migration Job
- Redis and PostgreSQL StatefulSets for small deployments

## Security & startup ordering

- All app workloads run with a hardened `securityContext`: `runAsNonRoot`,
  non-root uid/gid 1000, dropped Linux capabilities, `readOnlyRootFilesystem`
  (with a writable `/tmp` `emptyDir`), and the `RuntimeDefault` seccomp profile.
- API/worker/scheduler pods each have a `wait-for-migrations` initContainer that
  blocks until the migration Job has populated `alembic_version`. Because
  `kubectl apply -k` creates the Job and Deployments concurrently, this prevents
  app pods from serving against an un-migrated schema.

## Apply

Create the namespace and a real secret first:

```bash
kubectl apply -f deploy/k8s/namespace.yaml
cp deploy/k8s/secret.example.yaml /tmp/webhookwise-secret.yaml
$EDITOR /tmp/webhookwise-secret.yaml
kubectl apply -f /tmp/webhookwise-secret.yaml
kubectl apply -k deploy/k8s
```

Then check rollout:

```bash
kubectl -n webhookwise rollout status deploy/webhookwise-api
kubectl -n webhookwise rollout status deploy/webhookwise-worker
kubectl -n webhookwise rollout status deploy/webhookwise-scheduler
kubectl -n webhookwise get pods
```

Set `OTEL_EXPORTER_OTLP_ENDPOINT` in `configmap.yaml` to your cluster collector
before production rollout, for example `http://alloy.monitoring.svc:4317`.
The Kubernetes baseline enables OTLP logs, metrics, traces, schema URL
`https://opentelemetry.io/schemas/1.41.0`, trace-based metric exemplars, and
`parentbased_traceidratio` head sampling. Keep `OTEL_TRACES_SAMPLER_ARG`
aligned with the production traffic budget.

## Image Promotion

The default application image is pinned to `ghcr.io/itswl/webhookwise:3.3.0`. Override it per release:

```bash
kubectl -n webhookwise set image deploy/webhookwise-api webhookwise-api=ghcr.io/itswl/webhookwise:<release-tag>
kubectl -n webhookwise set image deploy/webhookwise-worker webhookwise-worker=ghcr.io/itswl/webhookwise:<release-tag>
kubectl -n webhookwise set image deploy/webhookwise-scheduler webhookwise-scheduler=ghcr.io/itswl/webhookwise:<release-tag>
```

Avoid `latest`; every deployed image should be a reproducible release tag or digest.

## Private registry pulls

`ghcr.io/itswl/webhookwise` is published to GHCR, whose packages are private by
default. If the package is not public, pods (and the `wait-for-migrations`
initContainers) fail with `ImagePullBackOff`. Either make the package public, or
create an image pull secret and attach it to the `webhookwise` ServiceAccount
(`serviceaccount.yaml`) via `imagePullSecrets` so every pod inherits it:

```bash
kubectl -n webhookwise create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username=<user> --docker-password=<token>
```
