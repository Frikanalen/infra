# MediaMTX contribution ingest

Environment wiring for the MediaMTX chart. The chart itself lives in
[Frikanalen/gitops `helm/mediamtx`](https://github.com/Frikanalen/gitops/tree/main/helm/mediamtx),
which documents its values, WebRTC paths, Opus preview workers and monitoring.

A single MediaMTX 1.21.3 instance receives authenticated SRT, RTMP and RTSP contributions.
Django owns the source UUIDs and credentials. MediaMTX calls
`http://django-api.default.svc.cluster.local:8080/api/mediamtx/auth` for every
publish/read request; an unavailable or rejecting callback fails closed.

The standard `argocd_app` role deploys the gitops chart via
`frikanalen_apps.mediamtx`; `app_versions.mediamtx` is the gitops revision Argo
tracks. Both app playbooks have a `mediamtx` tag. The MediaMTX image is pinned in
the chart, independently of either Git revision. The Application name and
Kubernetes resource names are still `mediamtx`, so re-running the role after the
move retargets the existing Application rather than creating a second service.

## Network

| Environment | Public SRT listener | Reserved LAN IP | RTSP (public hostname or LAN) |
| --- | --- | --- | --- |
| Staging | `srt://staging.frikanalen.no:8890` | `192.168.3.93` | `rtsp://192.168.3.93:8554` |
| Production | `srt://frikanalen.no:8890` | `192.168.3.107` | `rtsp://192.168.3.107:8554` |

PF forwards UDP 8890 (SRT), TCP 1935 (RTMP), and TCP 8554 (RTSP) from the
environment's existing public web IP to the new MetalLB address, including hairpin
access. RTSP media uses interleaved TCP; clients must select TCP transport, with
no separate RTP/RTCP UDP ports needed. HLS, MoQ, the management API,
and recording remain disabled. Django advertises the configured contribution and WebRTC
listeners in its one-time credential dialog.

Production URLs are `srt://frikanalen.no:8890`, `rtmp://frikanalen.no:1935` and
`rtsp://frikanalen.no:8554`. Staging uses `staging.frikanalen.no` on the same ports.
Each full URL includes the issued source path and the appropriate authentication.
These RTMP/RTSP listeners do not have TLS; use a trusted route/VPN when required.

The staging address is added to MetalLB's existing pool by the platform playbook.
The production address is already inside its existing `192.168.3.104/30` pool.
The new addresses are reserved in `data/hosts.yml`. One replica and Recreate
strategy avoid routing publishers and readers to different processes.

Authentication is source/action-scoped, with no anonymous-reader exception.
SRT authentication does not itself encrypt media or its stream ID; use a trusted
route/VPN for initial testing. Per-source transport encryption configuration is
separate work. Do not publish credential-bearing URLs in logs or issue comments.

## Prerequisites and deployment

Django must include django-api PR #81, migration `0042_streamcredential`, and the
MediaMTX callback before accepting a contribution. Both credential creation and
source administration are currently staff-only.

From infra, deploy staging in this order:

```sh
ansible-playbook playbooks/k8s_platform_dev.yml --tags metallb
ansible-playbook playbooks/k8s_apps_staging.yml --tags django,mediamtx
ansible-playbook playbooks/firewall.yml --tags dev
```

The Django app definition adds its internal service FQDN to ALLOWED_HOSTS and
sets `FK_MEDIAMTX_SRT_URL`, `FK_MEDIAMTX_RTMP_URL`, `FK_MEDIAMTX_RTSP_URL` and `FK_MEDIAMTX_WEBRTC_URL`. ArgoCD syncs the chart from gitops main after merge.
Check the Django rollout and its migration hook before trying credentials.

If using an unmerged Django branch for a staging trial, explicitly override both
the Django image and that app's ImageUpdater to the branch tag; otherwise the
updater can put main back. After PR #81 merges, rerun the Django app role to
restore main tracking. Production should use a released Django image containing
the callback; this change does not advance production's Django release pin.

## Test

Create an enabled source in staging admin, then issue two credentials for the
same source: Publish for the encoder and Read for the receiver. Copy the SRT
URLs from the one-time credential pages. No schedule booking is necessary.

For a test pattern, export the publishing URL as `PUBLISH_URL` and the reading
URL as `READ_URL` in your own shell without saving them to shell history. Then:

```sh
ffmpeg -re -f lavfi -i 'testsrc2=size=1280x720:rate=50' \
  -f lavfi -i 'sine=frequency=1000:sample_rate=48000' \
  -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p \
  -g 50 -b:v 4M -c:a aac -ar 48000 -ac 2 -f mpegts "$PUBLISH_URL"
```

For RTMP or RTSP, the credential dialog supplies the corresponding full URL.
Given a local H.264/AAC file, these commands test the alternative publishers:

```sh
ffmpeg -re -i test.mp4 -c copy -f flv "$RTMP_PUBLISH_URL"
ffmpeg -re -i test.mp4 -c copy -rtsp_transport tcp -f rtsp "$RTSP_PUBLISH_URL"
```

Read with `ffplay "$RTMP_READ_URL"` or
`ffplay -rtsp_transport tcp "$RTSP_READ_URL"`. RTSP must use TCP transport.

In another shell, read with `ffplay "$READ_URL"`. A publish key must not allow
reading. Revoke a key in admin and reconnect to verify rejection; revocation
alone does not disconnect existing sessions yet.

CasparCG can later consume the authenticated LAN RTSP URL for the same path;
this deployment does not change the broadcast schedule or on-air source.

## Fallback and capture

MediaMTX rejects `alwaysAvailable: true` on regex paths. The initial UUID-shaped
regex path therefore has fallback disabled. The chart accepts explicit `paths`
overrides: an exact `live/<UUID>` path may enable `alwaysAvailable: true` using
the default H.264/AAC stereo slate tracks. Provisioning those exact paths from
Django belongs to the next controller step. A disconnect currently ends the feed;
this is a reception/authentication test, not an on-air continuity deployment.

Recording is disabled, so no persistent volume or offline-slate recording grows
indefinitely. Capture storage and finalization are separate follow-ups.

## Validation and rollback

CI lints/renders the chart for both environments and rejects missing callback/IP
settings. Before applying, validate rendered objects with Kubernetes server-side
dry-run and compile the PF anchor with `pfctl -n`.

To stop this test service, remove its Argo application with resource deletion or
scale its deployment to zero after disabling automatic reconciliation. Remove
only contribution forwarding on UDP 8890 and TCP 1935/8554 from the environment's PF anchor; preserve existing web
and broadcast-stream rules. Do not remove an address from MetalLB while a service
still uses it. Existing broadcast output and production services are independent.

## Initial staging verification (29 October 2026)

Staging was deployed for testing from infra branch `codex/mediamtx-infrastructure`
and Django image tag `codex-mediamtx-auth` (django-api PR #81). Both the Django
Application image parameter and its ImageUpdater track that test tag. Only
staging's ingress_dev PF anchor was loaded; production was not changed.

An external FFmpeg publisher sent a synthetic 640x360 50fps H.264 picture and
48kHz stereo AAC to `staging.frikanalen.no:8890`. An independently authenticated
SRT reader received both tracks through the public forwarding path. Publish-only
credentials used for reading and a revoked read credential were rejected.
Temporary test organization, source and credentials were removed afterward.

After merging the prerequisite PRs, reapply the staging app definitions so the
MediaMTX Application tracks gitops main and Django/ImageUpdater track the main
image again. Until then, preserve the test branch overrides when reconciling
staging. Operators can issue their own source credentials in staging admin to
repeat the public SRT test; the SRT, RTMP and RTSP listeners and credential URLs are deployed in staging.

Staging listener URLs and ALLOWED_HOSTS are stored in the Django Application's
Helm values, matching `data/apps.yml`. Keep image-update parameters limited to
image fields: an image-updater reconciliation discarded temporary environment
parameters during the initial trial, causing HTTP 400 from the callback. Moving
the environment settings into Helm values fixed authentication across listeners.
The old failed staging migration Job was removed so Argo could recreate it;
both applications subsequently completed reconciliation successfully.

The follow-up public protocol matrix passed using a synthetic H.264 640x360
picture and stereo AAC audio:

| Publisher | Authenticated reader | Result |
| --- | --- | --- |
| SRT UDP 8890 | SRT UDP 8890 | Passed |
| SRT UDP 8890 | RTMP TCP 1935 | Passed |
| SRT UDP 8890 | RTSP TCP 8554 | Passed |
| RTMP TCP 1935 | SRT UDP 8890 | Passed |
| RTSP TCP 8554 | SRT UDP 8890 | Passed |

All connections used `staging.frikanalen.no`, exercising public forwarding and
Django authentication rather than an internal shortcut. RTSP used interleaved
TCP. The temporary test organization, source and credentials were removed after
testing. Production was not deployed or tested.

## WebRTC transport for source preview / return feeds

The environment app definition enables authenticated WebRTC. The gitops chart
routes `/live` and `/preview` on the site's own hostname to MediaMTX through a
standard Ingress, so `FK_MEDIAMTX_WEBRTC_URL` is the bare site origin:

- WHEP of the original: `https://<site>/live/<source UUID>/whep`
- WHEP of the Opus preview: `https://<site>/preview/<source UUID>/whep`

(The first staging trial used a `/live-preview` prefix and an Nginx sidecar from
the old infra chart; the gitops chart replaced both.)

WebRTC media uses UDP 8189 with TCP 8189 as a fallback, through the same
dedicated MediaMTX LoadBalancer and PF forwarding/hairpin rules. Only the public
hostname and reserved LAN IP are advertised. No TURN relay is deployed.

## Monitoring

MediaMTX's Prometheus endpoint is always enabled, on port 9998 behind the
internal `mediamtx-metrics` ClusterIP Service only; it is not forwarded by PF or
exposed by the LoadBalancer or Ingress. MediaMTX does not ask Django to authorize
metrics scrapes; every publish and read is still authorized.

Where `k8s_monitoring_enabled` is true (staging and prod), the chart also creates
a `mediamtx` ServiceMonitor scraped every 30 s. Like the other ServiceMonitors
here it carries no `release` label, because roles/kube_prometheus_stack selects
every ServiceMonitor in the cluster. To check the target after deploying, look
for `serviceMonitor/<namespace>/mediamtx/0` under Status > Targets on
`prometheus.<intern zone>`, or query `paths` in Grafana's Explore view.
