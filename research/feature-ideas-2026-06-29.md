# pingmon — feature analysis & roadmap (what's in demand, what's unique) — 2026-06-29

## Context

`pingmon` already does live per-country TCP latency/jitter/loss with a TUI,
sparkline trend, detail panel, sort-by-ms, and an editable target set. The user
asked: what other strong features are worth adding — analyse what the market
actually wants, and find something **unique and genuinely useful**, not filler.

The product's origin matters: the reference image was a **VPS "server location"
picker**. So the deepest unmet need is not "another ping graph" — it's
**"which region should I actually pick / route to right now?"**

## Method

Surveyed the popular terminal/network-latency tools and the features users
praise or request:

- **gping** — real-time multi-host latency graph; `--cmd` to graph any command. The bar is "ping but beautiful". [KX](https://kx.cloudingenium.com/en/gping-graphical-ping-real-time-graph-terminal-latency-guide/)
- **trippy** — mtr-class TUI: per-hop loss/jitter/AS, GeoIP, ICMP/UDP/TCP, Paris/Dublin traceroute, multi-target. [KX](https://kx.cloudingenium.com/en/trippy-traceroute-ping-network-diagnostic-tui-mtr-guide/)
- **mtr** — classic ping+traceroute; TCP/443 mode to punch through ICMP-blocking firewalls. [comparitech](https://www.comparitech.com/net-admin/best-ping-monitoring-tools/)
- **SmokePing** — the gold standard for *stability over time*: latency **distribution** ("smoke"), packet loss, and a **pattern-based alarm system** (e.g. fire when loss goes 1%→20% and stays there >10 min — kills duplicate alerts). [oss.oetiker.ch](https://oss.oetiker.ch/smokeping/)
- **VoIP/QoS monitors** — convert latency+jitter+loss into a single **MOS / R-factor** quality score via the ITU-T E-model; thresholds: latency <100 ms, jitter <20 ms, 0% loss = good; ~1% loss drops MOS ~0.4. [Obkio](https://obkio.com/blog/measuring-voip-quality-with-mos-score-mean-opinion-score/), [10-Strike](https://www.10-strike.com/network-monitor/help/monitoring/qos-jitter-monitoring.shtml)

## Findings

### What everyone already has (table stakes)
Real-time graph, multi-target, latency/jitter/loss, sort. `pingmon` covers these.

### Where the gaps are (opportunity)
1. **Nobody turns the raw numbers into a decision.** gping/trippy/mtr show data;
   none answer "which region is best for *my use case* right now". For a
   VPS-location tool that's the whole point.
2. **MOS / R-factor is absent from terminal tools** — it lives only in paid VoIP
   suites, yet it's the single most intuitive "is this link good?" number.
3. **Stability is under-shown.** Last-value + sparkline hides distribution;
   SmokePing's "smoke" band (min–p50–max) is what reveals a flaky link.
4. **Alerting in TUIs is weak.** SmokePing's pattern alarms are loved; CLI tools
   mostly just draw.
5. **No path drill-down from an overview.** trippy is great per-target but you
   start it per host; an overview→`Enter`→traceroute drill-down is missing.

## Recommendations (sorted by impact ÷ effort)

### ★ Unique pick — "Region Advisor" (composite score + use-case profiles)
The standout, directly tied to the VPS-picker origin. For each country compute a
**0–100 score** from latency + jitter + loss, under a selectable **profile**:

| Profile | Weighting bias |
| ------- | -------------- |
| VoIP / Video call | jitter & loss dominate; uses MOS/R-factor (E-model) |
| Gaming  | raw latency + jitter, loss heavily penalised |
| Web / API | latency-led, mild loss penalty |
| Bulk / backup | throughput-ish: loss-led, latency tolerant |

Show a ranked board with the **#1 recommended region highlighted** ("Best for a
video call now: 🇩🇪 Germany — MOS 4.3") and a one-line reason. This is unique
(no terminal tool does it), needed (answers the actual question), and cheap — it
reuses metrics already collected; only a scoring function + a profile toggle and
a small panel are new. **Highest value for the effort.**

### High value
- **MOS / R-factor column + gauge** (E-model: `R = 93.2 − Id(latency) − Ie(loss)`,
  effective-latency = `latency + 2·jitter`). One derived column; feeds the advisor.
- **SmokePing-style distribution band** in the detail panel: shade min–max with
  the median line over the history window — instantly shows stable vs jittery.
- **Threshold alerts with desktop/terminal notification**: per-target rule ("loss
  ≥ X% for N samples" / "latency > Y ms"), surfaced via terminal bell + OSC 9/777
  desktop notification, optional row flash. Pattern-based to avoid noise.

### Medium value
- **Traceroute drill-down**: `Enter` on a row opens an mtr-style hop view
  (loss/latency per hop) for that target. Combines pingmon's overview with
  trippy's depth.
- **GeoIP / ASN enrichment**: resolve the target IP to AS number + city/country
  (offline DB or a one-shot lookup) to confirm the host is really in-country.
- **History persistence + export**: append samples to a CSV/SQLite ring; export
  CSV/JSON; optional `--prometheus` text endpoint for homelab dashboards.
- **Multi-probe per tick**: send 3–5 connects per cycle → real min/avg/max and a
  truer loss%, like ICMP ping `-c`.

### Low / nice-to-have
- Per-target **uptime %** and "last seen up" timestamp.
- **A/B freeze**: snapshot a baseline and show deltas (before/after a route change).
- **Theme presets** and a compact/dense mode.
- **Sound on outage** toggle.

## Actions

- Implemented this session (separately): sort-by-ms toggle (`m`/`M`).

### Implemented 2026-06-29 (user picked all four top items)
- **Region Advisor + MOS/R-factor** — `pingmon/scoring.py` (profiles VoIP/Gaming/
  Web/Bulk, 0–100 score, grade, reason) and `TargetStats.r_factor` / `.mos` via
  the ITU-T E-model in `pingmon/stats.py`. Surfaced as a ranked modal (`g`,
  profile switch `[`/`]`/`p`/`Tab`), a MOS row in the detail panel, and a
  "best region" hint in the banner.
- **Threshold alerts** — `_is_bad` / `_check_alerts` / `_raise_alert` in
  `app.py`; config keys `alert_latency` / `alert_loss` / `alert_window` /
  `desktop_notify`. Bell + toast + OS desktop notification (`netutil.desktop_notify`,
  macOS osascript / Linux notify-send) + blinking row marker, auto-clear on recovery.
- **Traceroute drill-down** — `netutil.traceroute` (async, macOS/Linux/Windows)
  + `TracerouteScreen`, opened with `Enter`/click; per-hop loss and per-probe ms.
- **Distribution band** — partially covered: the detail panel already shows the
  Textual live sparkline + min/max; a dedicated SmokePing-style shaded band is
  still open (the user grouped it under "distribution" but min/max/median is the
  remaining delta). Tracked as a follow-up.

- Still deferred: GeoIP/ASN enrichment, history persistence + CSV/Prometheus
  export, multi-probe per tick, full SmokePing-style median/quartile band.

## Sources
- gping — https://kx.cloudingenium.com/en/gping-graphical-ping-real-time-graph-terminal-latency-guide/
- trippy — https://kx.cloudingenium.com/en/trippy-traceroute-ping-network-diagnostic-tui-mtr-guide/
- ping tool roundup — https://www.comparitech.com/net-admin/best-ping-monitoring-tools/
- SmokePing — https://oss.oetiker.ch/smokeping/
- MOS score (Obkio) — https://obkio.com/blog/measuring-voip-quality-with-mos-score-mean-opinion-score/
- QoS/jitter/MOS (10-Strike) — https://www.10-strike.com/network-monitor/help/monitoring/qos-jitter-monitoring.shtml
