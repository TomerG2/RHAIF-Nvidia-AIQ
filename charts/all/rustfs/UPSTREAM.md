The unmodified official chart `rustfs-1.0.1.tgz` is from
https://charts.rustfs.com/rustfs-1.0.1.tgz.

Archive SHA-256: `030d82b6bb3a4e90c5c25a2f260d49da0681fc87385647dc28d77d2314976233`.
Image: `quay.io/rustfs/rustfs:1.0.1@sha256:1803faef57627e2d9c2e7d89d655d712ddded5389040054987163043fecb6a3c`.
Resolved on 2026-10-05; image source revision
`6de965ae3c965a78ff819fbcd7acd4aa44177d92`.

The release Dockerfile sets UID/GID 10001 and mode 0750 on `/data` and `/logs`.
The wrapper scopes an SCC to this identity and service account. The chart invokes
the binary directly, bypassing the entrypoint's attempted ownership changes.
The chart's TLS option also enables client authentication and TLS key logging;
explicit environment overrides disable those two features while retaining TLS,
certificate management, HTTPS peers, and probes. SigV4 authenticates S3 requests.
The credential bootstrap copies the CA to client namespaces; a CronJob refreshes it.

Do not edit the archive. Upgrade the dependency, digest, and these notes together.
