# Per-country target host selection for pingmon — 2026-06-29

## Context

Building `pingmon`, a TUI to measure latency/availability to the VPS countries
shown in the reference image (Netherlands, Germany, United Kingdom, France,
Cyprus, Italy, Spain, Greece, Sweden, Ireland) plus the United States. The app
measures latency with **TCP connect timing** (no root), so every country needs
at least one publicly reachable host that answers on a TCP port (443 or 80).
The goal was a built-in, working default set, with the config remaining
user-editable.

## Method

Probed candidate hosts with `nc -z -w 2 <host> <port>` in parallel, timing the
connect. Two sweeps:

1. Port 443 across speedtest/looking-glass providers (Leaseweb, Clouvider,
   Linode, Hetzner, Tele2).
2. Port 80 for the hosts that failed on 443 (most speedtest mirrors only serve
   HTTP) plus extra national mirrors for the countries still missing.

Reachability only — latency numbers from the probe were indicative, not the
selection criterion.

## Findings

- Many speedtest mirrors **only listen on port 80/8080**, not 443
  (`speed.hetzner.de`, `speedtest.tele2.net`, all Leaseweb `speedtest.*`
  mirrors). Probing 443 alone produced false negatives.
- Provider naming schemes are inconsistent: Linode `speedtest.<city>.linode.com`
  works for Frankfurt/London/Newark but has no Amsterdam/Paris PoP; Clouvider
  `<city>.speedtest.clouvider.net` works for London/LA but several city codes
  don't resolve.
- Smaller countries (Cyprus, Greece, Ireland) have no big-provider speedtest
  host; **national academic mirrors** are the reliable option
  (`ftp.cs.ucy.ac.cy`, `ftp.ntua.gr`, `ftp.heanet.ie`).

### Confirmed reachable set (used as defaults)

| Country        | Host                              | Port |
| -------------- | --------------------------------- | ---- |
| Netherlands    | speedtest.ams1.nl.leaseweb.net    | 80   |
| Netherlands    | ams.speedtest.clouvider.net       | 80   |
| Germany        | speedtest.frankfurt.linode.com    | 443  |
| Germany        | ftp.fau.de                        | 80   |
| United Kingdom | speedtest.london.linode.com       | 443  |
| United Kingdom | lon.speedtest.clouvider.net       | 443  |
| France         | scaleway.testdebit.info           | 80   |
| Cyprus         | ftp.cs.ucy.ac.cy                  | 80   |
| Cyprus         | mirror.library.ucy.ac.cy          | 80   |
| Italy          | mirror.garr.it                    | 80   |
| Italy          | giano.com.dist.unige.it           | 80   |
| Spain          | ftp.cica.es                       | 80   |
| Greece         | ftp.ntua.gr                       | 80   |
| Greece         | ftp.cc.uoc.gr                     | 80   |
| Sweden         | speedtest.tele2.net               | 80   |
| Ireland        | ftp.heanet.ie                     | 80   |
| United States  | speedtest.newark.linode.com (east)| 443  |
| United States  | la.speedtest.clouvider.net (west) | 443  |
| United States  | mirror.us.leaseweb.net            | 80   |

### Hosts that failed and were dropped

`speed.hetzner.de:443`, `speedtest.tele2.net:443`, `speedtest.*.leaseweb.net:443`
(HTTP-only), `nl/par/mad/mil/ie.speedtest.clouvider.net` (don't resolve),
`paris.testdebit.info`, `speedtest.milano.aruba.it`, `mirror.fr/uk.leaseweb.net`.

## Recommendations

1. **Keep two hosts per major country** where available — gives a sanity check
   if one mirror has a bad day. Done for NL, DE, UK, CY, IT, GR, US.
2. **Default the US to two coasts** (Newark east, LA west) so the dashboard
   reflects geographic spread, not just one PoP. Done.
3. The probe is reachability-only; do **not** treat the built-in hosts as
   authoritative for the country's typical latency — they are datacenter/mirror
   endpoints. For monitoring a specific VPS, add its own IP via `a` or the
   config.
4. Re-validate the set periodically: academic/mirror hosts occasionally change
   ports or retire. A `make check-hosts` style probe could be added later.

## Actions

- Implemented: the confirmed set is hard-coded as `DEFAULT_TARGETS` in
  `pingmon/config.py`; written to `config.toml` on first run and fully editable.
- Implemented: in-app add/delete of targets, so the default set is a starting
  point, not a constraint.
- Deferred: an automated host-reachability re-check (no scheduler yet).
