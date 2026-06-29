# Day 23 Lab Reflection

> Fill in each section. Grader reads the "What I'd change" paragraph closest.

**Student:** Nguyen Thanh Toan
**Student ID:** 2A202600633
**Submission date:** 2026-06-29
**Lab repo URL:** https://github.com/ttoannguyen/2A202600633-Nguyen-Thanh-Toan-Day23-Track2-Observability-Lab

---

## 1. Hardware + setup output

Output of `python 00-setup/verify-docker.py` (this authoring machine has **no Docker**, so the
pre-flight honestly reports `docker: FAIL`. Re-run on the Docker-capable host before final submission —
the report file is regenerated each run):

```
Docker:        FAIL  (docker binary not found in PATH)
Compose v2:    FAIL  (skipped: docker unavailable)
RAM available: 0.0 GB (NEED >= 4.0 GB)
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

Drop `submission/screenshots/dashboard-overview.png`.

The overview dashboard (`ai-service-overview.json`, lints OK) carries the RED + USE + 4th-pillar split:
1. **Request rate** — `rate(inference_requests_total[1m])` (R)
2. **Error ratio** — `rate(inference_requests_total{status="error"}[5m]) / rate(inference_requests_total[5m])` (E)
3. **Latency P50/P95/P99** — `histogram_quantile(0.99, rate(inference_latency_seconds_bucket[5m]))` (D)
4. **In-flight requests** — `inference_active_gauge` (U — saturation; rises under load, returns to 0)
5. **GPU utilization** — `gpu_utilization_percent` (USE)
6. **Quality score** — `inference_quality_score` (4th pillar / eval-as-metric)

### Burn-rate panel

Drop `submission/screenshots/slo-burn-rate.png`. (`slo-burn-rate.json` lints OK.)

### Alert fire + resolve

| When | What | Evidence |
|---|---|---|
| _T0_ | killed `day23-app` (`make alert` → `trigger-alert.sh`) | screenshot `alertmanager-firing.png` |
| _T0+90s_ | `ServiceDown` fired (Prometheus `up{job="app"} == 0` for 1m → Alertmanager → Slack) | screenshot `slack-firing.png` |
| _T1_ | restored app | — |
| _T1+60s_ | alert resolved (Alertmanager sends `[RESOLVED]`) | screenshot `slack-resolved.png` |

> Runtime-measured timestamps + screenshots to be captured on the Docker host; the fire/resolve
> *mechanism* (kill container → `up==0` → `for: 1m` → route to Slack receiver → restore → resolve) is
> wired in `prometheus/rules/*.yml` + `alertmanager/alertmanager.yml`.

### One thing surprised me about Prometheus / Grafana

`up` is a metric Prometheus **synthesizes itself** from each scrape — I didn't have to emit it from the
app. That means the most important reliability alert (`ServiceDown`) needs zero app-side
instrumentation; it works precisely *because* the target stopped answering. The thing you most need to
alert on is the absence of data, not a value in the data.

---

## 3. Track 03 — Tracing & Logs

### One trace screenshot from Jaeger

Drop `submission/screenshots/jaeger-trace.png` showing `embed-text → vector-search → generate-tokens`
spans. The `POST /predict` handler (`01-instrument-fastapi/app/main.py`) opens a parent `predict` span
and three child spans, so each trace is a 1-parent / 3-child tree. The `generate-tokens` span carries
GenAI semantic-convention attributes: `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.response.finish_reason`.

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
