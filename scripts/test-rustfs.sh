#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
work=$(mktemp -d /tmp/aiq-rustfs.XXXXXX)
name="aiq-rustfs-test-${RANDOM}"
rustfs=quay.io/rustfs/rustfs@sha256:1803faef57627e2d9c2e7d89d655d712ddded5389040054987163043fecb6a3c
tools=docker.io/kserve/storage-initializer@sha256:2f0c1859bc1eac0504190fb4047a13206e1c26692463b5ccdc382fa694e07db8
cleanup() { podman rm -f "$name" >/dev/null 2>&1 || true; rm -rf -- "$work"; }
trap cleanup EXIT
mkdir -p "$work/tls" "$work/credentials/reader" "$work/credentials/publisher" "$work/source"
cp -R "$repo/charts" "$repo/tests" "$work/source/"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$work/tls/rustfs_key.pem" \
  -out "$work/tls/rustfs_cert.pem" -days 1 -subj /CN=localhost \
  -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' >/dev/null 2>&1
cp "$work/tls/rustfs_cert.pem" "$work/tls/ca.crt"
chmod -R a+rX "$work"
for identity in reader publisher; do
  printf 'aiq-test-%s' "$identity" > "$work/credentials/$identity/AWS_ACCESS_KEY_ID"
  printf 'isolated-integration-test-%s' "$identity" > "$work/credentials/$identity/AWS_SECRET_ACCESS_KEY"
done
podman run -d --name "$name" --user 10001:10001 --read-only --read-only-tmpfs=false \
  --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /data:rw,mode=777 \
  -v "$work/tls:/tls:ro,Z" -p 127.0.0.1::9000 \
  -e RUSTFS_ACCESS_KEY=aiq-test-admin -e RUSTFS_SECRET_KEY=isolated-integration-test-admin \
  -e RUSTFS_TLS_PATH=/tls -e RUSTFS_SERVER_MTLS_ENABLE=0 -e RUSTFS_TLS_KEYLOG=0 \
  -e RUSTFS_OBS_LOG_DIRECTORY= --entrypoint /usr/bin/rustfs "$rustfs" >/dev/null
port=$(podman port "$name" 9000/tcp | sed 's/.*://')
ready=false
for attempt in {1..60}; do
  if curl --cacert "$work/tls/ca.crt" -fsS "https://localhost:$port/health/ready" >/dev/null 2>&1; then ready=true; break; fi
  sleep 1
done
if [[ $ready != true ]]; then podman logs "$name"; exit 1; fi
podman run --rm --network=host --entrypoint python \
  -v "$work/source:/source:ro,Z" -v "$work/tls:/tls:ro,z" -v "$work/credentials:/credentials:ro,Z" \
  -e "S3_ENDPOINT=https://localhost:$port" -e S3_BUCKET=aiq-test-models -e S3_PREFIX=snapshots \
  -e AWS_DEFAULT_REGION=us-east-1 -e AWS_CA_BUNDLE=/tls/ca.crt \
  -e RUSTFS_ACCESS_KEY=aiq-test-admin -e RUSTFS_SECRET_KEY=isolated-integration-test-admin \
  -e MODEL_MAX_BYTES=10485760 -e DISK_RESERVE_BYTES=1048576 \
  "$tools" /source/tests/integration/rustfs_smoke.py
