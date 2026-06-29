# Day 23 Lab Reflection

> Fill in each section. Grader reads the "What I'd change" paragraph closest.

**Student:** Nguyen Thanh Toan
**Student ID:** 2A202600633
**Submission date:** 2026-06-29
**Lab repo URL:** https://github.com/ttoannguyen/2A202600633-Nguyen-Thanh-Toan-Day23-Track2-Observability-Lab

---

## 1. Hardware + setup output

Stack runs on **Docker Engine inside WSL2 (Ubuntu-24.04)** — Docker Desktop is not installed on this
Windows host, so I installed `docker-ce` + Compose v2 in the WSL distro and run the lab from
`/mnt/d/...`. Ports bind to `127.0.0.1` in WSL and WSL2's localhost-forwarding makes them reachable from
Windows (`make verify` runs from Windows and passes). Pre-flight output (`python3 00-setup/verify-docker.py`,
stack down so ports are free):

```
Docker:        OK  (29.6.1)
Compose v2:    OK  (5.2.0)
RAM available: 7.61 GB (OK)
Ports free:    OK
Report written: 00-setup/setup-report.json
```

### App-layer proof (ran standalone, no Docker)

I ran `uvicorn main:app` directly under Python 3.13 and exercised it. All 6 metric families are live, and
`verify.py` passes both `01:` app checkpoints (`/healthz` reachable, `/metrics` exposes
`inference_requests_total`). Real `/metrics` scrape after 4 ok + 1 forced-error request:

```
inference_requests_total{model="llama3-mock",status="ok"} 4.0
inference_requests_total{model="llama3-mock",status="error"} 1.0
inference_latency_seconds_count{model="llama3-mock"} 4.0
inference_active_gauge 0.0
inference_tokens_total{direction="input",model="llama3-mock"} 16.0
inference_tokens_total{direction="output",model="llama3-mock"} 248.0
inference_quality_score{model="llama3-mock"} 0.822
gpu_utilization_percent 82.6
```

The forced-error request (`{"fail": true}`) returned **HTTP 503** and incremented the `status="error"`
counter — this is the same path `make alert` drives. `inference_active_gauge` returns to `0.0` once
in-flight requests drain (rises under concurrent `make load`).

> Note: I patched `verify-docker.py` so the pre-flight no longer crashes when the `docker` binary is
> absent — `check_compose_v2()` / `check_ram_headroom()` shelled out unconditionally and raised
> `FileNotFoundError [WinError 2]`, which meant `setup-report.json` was never written. The script now
> short-circuits those two probes when `check_docker()` fails and still emits the report. On the grading
> host (Docker present) behaviour is unchanged.

---

## 2. Track 02 — Dashboards & Alerts

### 6 essential panels (screenshot)

Screenshot: [`submission/Grafana.png`](Grafana.png).

The overview dashboard (`ai-service-overview.json`, lints OK) carries the RED + USE + 4th-pillar split:
1. **Request rate** — `rate(inference_requests_total[1m])` (R)
2. **Error ratio** — `rate(inference_requests_total{status="error"}[5m]) / rate(inference_requests_total[5m])` (E)
3. **Latency P50/P95/P99** — `histogram_quantile(0.99, rate(inference_latency_seconds_bucket[5m]))` (D)
4. **In-flight requests** — `inference_active_gauge` (U — saturation; rises under load, returns to 0)
5. **GPU utilization** — `gpu_utilization_percent` (USE)
6. **Quality score** — `inference_quality_score` (4th pillar / eval-as-metric)

### Burn-rate panel

Screenshot: [`submission/Grafana-SLO.png`](Grafana-SLO.png). Cost dashboard:
[`submission/Grafana-cost.png`](Grafana-cost.png). (`slo-burn-rate.json` lints OK.)

### Alert fire + resolve

Ran for real against the live stack (Docker Engine in WSL2). Measured from the Alertmanager
`/api/v2/alerts` API:

| When | What | Measured |
|---|---|---|
| T0 | killed `day23-app` (`docker stop`, same path as `make alert`) | — |
| **T0+100s** | `ServiceDown` became `active` in Alertmanager — payload: `ServiceDown \| active \| inference-api is down` | screenshot `alertmanager-firing.png` |
| T1 | restarted app (`docker start`) | — |
| **T1+30s** | alert cleared (`active=0`) | screenshot `alertmanager-firing.png` (resolved) |

Fire latency (~100s) = scrape interval + the rule's `for:` hold before Prometheus promotes the alert to
firing and pushes it to Alertmanager. Resolve was faster (~30s) because once `up` returns to 1 the
condition is immediately false.

**Slack delivery (checkpoint 11) — proven end-to-end without a Slack account.** I have no real Slack
workspace, so instead of `hooks.slack.com` I pointed `SLACK_WEBHOOK_URL` at a local `slack-catcher`
container on the obs network and captured the actual POSTs Alertmanager makes. Two deliveries landed:
`[FIRING:1] ServiceDown` (color `danger`) and, after restart, `[RESOLVED] ServiceDown` (color `good`) —
the exact Slack-formatted JSON Slack would render. Full payloads in
[`submission/slack-delivery-proof.txt`](slack-delivery-proof.txt). To get the literal Slack-UI
screenshots, drop a real webhook into `.env` and re-run `make alert`; the receiver wiring is identical.

### One thing surprised me about Prometheus / Grafana

`up` is a metric Prometheus **synthesizes itself** from each scrape — I didn't have to emit it from the
app. That means the most important reliability alert (`ServiceDown`) needs zero app-side
instrumentation; it works precisely *because* the target stopped answering. The thing you most need to
alert on is the absence of data, not a value in the data.

---

## 3. Track 03 — Tracing & Logs

### One trace screenshot from Jaeger

Screenshots: [`jaeger-trace.png`](jaeger-trace.png), [`jaeger-predict.png`](jaeger-predict.png),
[`jaeger-generate-tokens.png`](jaeger-generate-tokens.png) (the last shows the GenAI attrs). **Real
trace captured** from Jaeger's
`/api/traces?service=inference-api` after load — one trace, 4 spans, correct parent/child tree:

```
trace 760aad4c2d93f76e9f151f48328523b5  (4 spans)
  predict            <- (root)
    embed-text       <- parent: predict
    vector-search    <- parent: predict
    generate-tokens  <- parent: predict
```

The `generate-tokens` span carried GenAI semantic-convention attributes, read straight off the span:

```
gen_ai.usage.input_tokens   = 4
gen_ai.usage.output_tokens  = 31
gen_ai.response.finish_reason = stop
```

(parent `predict` also carries `gen_ai.request.model`). Jaeger listed both services: `inference-api`
and `jaeger-all-in-one`. Note: this only worked after I fixed the handler — see §6.

### Log line correlated to trace

`main.py` logs the same `trace_id` it returns in the response body, so a Loki log line joins to its
Jaeger trace on `trace_id`. **Real line** captured from a local `uvicorn main:app` run + one
`POST /predict` (structlog `JSONRenderer` to stdout):

```json
{"model": "llama3-mock", "input_tokens": 4, "output_tokens": 62, "quality": 0.822, "duration_seconds": 0.2765, "trace_id": "bce83df6e908c9f542c48c3e3a89c167", "event": "prediction served", "level": "info", "timestamp": "2026-06-29T14:23:20.613199Z"}
```

The `/predict` HTTP response carried the matching `"trace_id":"bce83df6e908c9f542c48c3e3a89c167"`, so the
log line and the Jaeger trace join on that exact 32-hex id. (On the Docker host the same line appears in
`docker compose logs app` and the id is searchable in Jaeger.)

### Tail-sampling math

Policies (from `otel-collector/sampling-policies.md` + `otel-config.yaml`): keep **100%** of error
traces, **100%** of slow traces (latency > 2s), and **1%** of healthy traces.

For `N` traces/sec, with typical web mix (1% error, 1% slow-and-not-error, 98% healthy):

```
sampled = N × (P(error)·1.0 + P(slow∧¬error)·1.0 + P(healthy)·0.01)
        = N × (0.01 + 0.01 + 0.98·0.01)
        = N × 0.0298
        ≈ 3.0% retained  →  ~97% storage reduction
```

The **forced-error** trace I generate with `{"fail": true}` returns HTTP 503 → span status `ERROR` →
matched by the `keep-errors` policy → **retained** at 100%. A baseline healthy trace falls under the
`probabilistic-1pct` policy → **dropped** 99 times out of 100. That is the design goal: pay storage for
the traces that carry incident signal, throw away the boring ones — but never let the 1% probabilistic
policy swallow an error (anti-pattern called out in the policy doc).

Buffer cost: `decision_wait: 30s` × `num_traces: 50000` ≈ 50 MB RAM, holding ~30s of traffic, so the
single-collector ceiling is ~50000/30 ≈ **1666 traces/sec** before the circular buffer overflows.

**Observed on the live stack:** I sent 400 healthy + 5 forced-error requests. After the 30s
`decision_wait`, Jaeger held **all 5 error traces (100%)** and **3 of 400 healthy traces (≈0.75%, i.e.
the 1% policy)** — exactly the policy split predicted above. The `decision_wait` is also why traces only
appear ~30s after the request: the collector buffers every span until it can make the keep/drop decision.

---

## 4. Track 04 — Drift Detection

### PSI scores

`04-drift-detection/reports/drift-summary.json` (generated by `make drift`, seed=42, reference vs.
deliberately-shifted current; reference `prompt_length` N(50,15)→N(85,20), `response_quality`
Beta(8,2)→Beta(2,6)):

```json
{
  "prompt_length":    { "psi": 3.461,  "kl": 1.7982,  "ks_stat": 0.702, "ks_pvalue": 0.0,      "drift": "yes" },
  "embedding_norm":   { "psi": 0.0187, "kl": 0.0324,  "ks_stat": 0.052, "ks_pvalue": 0.133853, "drift": "no"  },
  "response_length":  { "psi": 0.0162, "kl": 0.0178,  "ks_stat": 0.056, "ks_pvalue": 0.086899, "drift": "no"  },
  "response_quality": { "psi": 8.8486, "kl": 13.5011, "ks_stat": 0.941, "ks_pvalue": 0.0,      "drift": "yes" }
}
```

The two intentionally-shifted features cross the PSI > 0.2 threshold by orders of magnitude
(`prompt_length` 3.46, `response_quality` 8.85); the two unchanged features sit near zero (0.018, 0.016)
and their KS p-values (0.13, 0.09) **fail to reject** the null "same distribution." The math correctly
separates real drift from sampling noise.

### Which test fits which feature?

| Feature | Type | Test I'd use in prod | Why |
|---|---|---|---|
| `prompt_length` | continuous, unbounded count | **KS** (+ PSI for alerting) | KS is non-parametric, needs no binning, and is sensitive to a location shift like 50→85. PSI gives an ops-friendly bucketed score to threshold on. |
| `embedding_norm` | continuous, high-dimensional proxy | **MMD** | Embedding norm is a 1-D shadow of a high-D vector; MMD (kernel two-sample) detects distribution change in the embedding space that a per-feature 1-D test would miss. For the scalar norm alone, KS also works. |
| `response_length` | continuous count | **PSI** | Cheap, bucketed, stable to monitor continuously; a length blow-up is exactly the "fat-tail moved" case PSI bins catch. |
| `response_quality` | bounded [0,1], skewed (Beta) | **KL / PSI on fixed bins** | KS overstates significance on bounded skewed data; KL on a fixed [0,1] binning captures the Beta(8,2)→Beta(2,6) mass shift (high-quality → low-quality) directly — here KL=13.5, unmistakable. |

Rule of thumb: **KS** for "did a continuous distribution move?" (no params), **PSI** for "give me a
single number to alert on" (binned, ops-friendly), **KL** for "how much information changed?" on
bounded/probability-like features, **MMD** for high-dimensional / embedding drift where 1-D tests are blind.

---

## 5. Track 05 — Cross-Day Integration

### Which prior-day metric was hardest to expose? Why?

**Wired for real:** I connected Day 19 as a stub exporter — `monitor-day19-vector-store.py` runs as the
`day19-stub` service on the obs network, emits `day19_qdrant_collections=3` on `:9101`, Prometheus
scrapes it (job `day19-stub`), and the cross-day dashboard's Day-19 panel renders that value; the other
five panels (Days 16/17/18/20/22) fail-soft to "No Data". So #19 (≥1 source connected) and #20 (6 panels
render) are met with one real source + graceful empties.

Day 20 (llama.cpp serving) is the hardest. The integration script
(`05-integration/monitor-day20-llama-cpp.py`) has to scrape GPU/serving telemetry that the serving
runtime doesn't natively expose in Prometheus format — you end up wrapping the server or parsing its
logs, and GPU utilization is sampled from outside the process. Day 19 (vector store) is easier because
the store already speaks a metrics endpoint; Day 20's value (tokens/sec, KV-cache occupancy, real GPU%)
lives inside a C++ process that wasn't built to be scraped, so the metric you most want (cost-driving
GPU saturation) is the one with the weakest seam to attach to.

---

## 6. The single change that mattered most

The change that flipped this stack from "works" to "useful" was promoting **quality** to a first-class
signal — the `inference_quality_score` gauge (the lab's "4th pillar," deck §2 LLM-Native Signals) — and
then wiring drift on top of it (deck §10). Everything else on the overview dashboard is classic RED/USE:
request rate, error ratio, latency, saturation. But the opening question of this lab is *"model ran fine
yesterday — today accuracy drops 20%, would you catch it, and how fast?"* RED/USE answers **none** of
that. A model can return HTTP 200 in 30ms with healthy GPU and a flat error rate while quietly producing
garbage. Latency and error count are blind to semantic regression by construction. The quality gauge is
the only signal on the board that moves when the *content* degrades rather than the *plumbing*.

Concretely: by emitting `inference_quality_score` per request and pairing it with the offline PSI/KL/KS
drift job, a quality regression becomes observable two ways — live (the gauge sags on the dashboard) and
distributional (the `response_quality` PSI jumps to 8.85, far past the 0.2 alert threshold). That second
path is what makes it *useful* instead of merely *visible*: a single bad request is noise, but a PSI
spike across 1000 requests is a defensible alert that says "the distribution moved, not just one call."
If I had to defend one design decision to an on-call reviewer, it's this — I refused to let the dashboard
imply the system was healthy just because the 200s were fast. The label budget stayed cheap to afford it:
`inference_requests_total` carries only `{model, status}` (deck §3 cardinality discipline), so adding a
semantically-rich quality signal cost almost nothing in series count while answering the one question the
whole lab is built around.

---

## Appendix — bugs I fixed to get the stack green (WSL2 Docker)

I ran the full 7-service stack on Docker Engine inside WSL2 (Ubuntu-24.04); `make verify` →
**12/12 checkpoints pass**. Four real defects blocked it:

1. **`verify-docker.py` crashed with no Docker** — `check_compose_v2()`/`check_ram_headroom()` shelled
   out to `docker` unconditionally even after `check_docker()` failed → `FileNotFoundError`, so
   `setup-report.json` was never written. Added an `if docker_ok:` guard.
2. **Alertmanager wouldn't boot** — `alertmanager.yml` used `api_url: '{{ env "SLACK_WEBHOOK_URL" }}'`,
   but Alertmanager does **not** expand env vars in `api_url` (it's parsed as a URL at config-load) →
   `unsupported scheme "" for URL`, container `Exited (1)`. Switched to `api_url_file: '/tmp/slack_url'`
   and a compose `entrypoint` that writes `$SLACK_WEBHOOK_URL` to that file at boot. Env-driven, no
   secret in the repo, boots even with the placeholder.
3. **Traces never formed a tree** — the handler created the parent with `tracer.start_span("predict")`
   (never made *current*), so `embed-text`/`vector-search`/`generate-tokens` had no active parent and
   each exported as its **own single-span root trace**. Jaeger showed only 1-span traces. Wrapped the
   body in `with tracer.start_as_current_span("predict")` so the 3 children nest → the
   "POST /predict + 3 child spans" tree the rubric asks for (verified above).
4. **`keep-errors` tail-sampling couldn't see errors** — the forced-failure path raised a 503 but never
   set the span status, so the span was `UNSET`, not `ERROR`; the `keep-errors` policy didn't match and
   error traces fell to the 1% policy. Added `span.set_status(Status(StatusCode.ERROR, ...))`. After the
   fix, all 5 error traces were retained 100% (measured).

Plus a portability fix: the `.sh` scripts were checked out **CRLF** on Windows, so `bash` failed with
`$'\r': command not found`. Added `.gitattributes` (`*.sh text eol=lf`) and renormalized the scripts.
