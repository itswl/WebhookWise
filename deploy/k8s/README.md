# WebhookWise Kubernetes Manifests

These manifests provide a production-shaped baseline for running WebhookWise on Kubernetes:

- API deployment with `/live` and `/ready` probes
- horizontally scalable TaskIQ worker deployment
- single-replica scheduler deployment
- migration Job
- Redis and PostgreSQL StatefulSets for small deployments

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
before production rollout, for example `http://alloy.monitoring.svc:4318`.

## Image Promotion

The default application image is pinned to `ghcr.io/itswl/webhookwise:0.1.0`. Override it per release:

```bash
kubectl -n webhookwise set image deploy/webhookwise-api webhookwise-api=ghcr.io/itswl/webhookwise:<release-tag>
kubectl -n webhookwise set image deploy/webhookwise-worker webhookwise-worker=ghcr.io/itswl/webhookwise:<release-tag>
kubectl -n webhookwise set image deploy/webhookwise-scheduler webhookwise-scheduler=ghcr.io/itswl/webhookwise:<release-tag>
```

Avoid `latest`; every deployed image should be a reproducible release tag or digest.
