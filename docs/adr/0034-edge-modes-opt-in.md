# ADR-0034: Edge modes are opt-in: a certificate from files, an extra host, a Cloudflare tunnel in the stack

- **Status:** Proposed in M13.1. The maintainer decided on 2026-11-28 where the tunnel's connector runs (point 3, Q76), that Cloudflare terminating TLS is exception X16 (point 8, Q77) and that the token links stay behind Access (point 9). The record is accepted at the review of the M13 pull request
- **Date:** 2026-11-28
- **Governs:** `docker_compose/gmat_m8/caddy/Caddyfile`, `docker_compose/gmat_m8/docker-compose.yml`, `docker_compose/gmat_m8/docker-compose.production.yml`, `docker_compose/gmat_m8/ops/checks.py`, `apps/core/http.py`

## Context

The pilot host is reached in two ways: through Cloudflare Tunnel with Cloudflare Access in front, and on the LAN by the host's address. Its Caddy serves a certificate read from files and runs as uid 1000 with no capability. All of that lives outside the checkout. A local script regenerates the Caddyfile from the release's, finding each edit by an exact anchor, and a local compose overlay does the rest. Any release can break them silently.

Until now Caddy believed no forwarded header ([ADR-0008](0008-caddy-whitenoise.md)): the visitor was the connection's peer, and Django believes the one address Caddy writes (`ALLAUTH_TRUSTED_PROXY_COUNT = 1`, `apps.core.http.client_ip`). A tunnel breaks that. Every visitor reaches Caddy from the connector's address, so the per-address sign-in limits would treat everyone as one visitor. And the visitor's TLS ends at Cloudflare.

What follows was checked on 2026-11-28 against Caddy 2.11.9 (the pinned image), against `cloudflared`'s source, and against Cloudflare's current documentation ([`docs/security.md`](../security.md), *The edge*).

## Decision

1. **Three opt-in settings, all off by default.** `.env` keys, with final names:
   - `EDGE_TLS`: `acme` (the default) or `files`;
   - `EDGE_EXTRA_HOST`: one more host name for the site, empty by default;
   - `EDGE_PROXY`: `none` (the default) or `cloudflare`;
   - `EDGE_TUNNEL_SUBNET` and `EDGE_TUNNEL_ADDRESS`: the tunnel's network and the connector's fixed address on it. They are read only when `EDGE_PROXY=cloudflare`, and their defaults are M13.3's.

   An installation that sets none of these keys gets the Caddy configuration it had before they existed, apart from the two `header_up` lines of point 4. M13.3 compares `caddy adapt` before and after its change.
2. **Caddy picks each mode by importing a file named after its value.** Caddy replaces `{$VAR}` before it parses, so `import edge/tls-{$EDGE_TLS}.caddy` and `import edge/proxy-{$EDGE_PROXY}.caddy` choose a file. Each file defines **named snippets**, and the Caddyfile imports them where they apply: the global options, the `servers` block and the site. As a result, a value names exactly one file or Caddy refuses to start (measured):
   - a missing file is an error;
   - a value with a glob character that matches no file is only a warning to `import`, but it leaves the snippets undefined, and their use is then an error;
   - a value that matches two files declares the snippets twice, which is an error too.

   A variable that is set but empty does not take Caddy's `{$VAR:default}` (it names `tls-.caddy`), so compose passes `${EDGE_TLS:-acme}` and `${EDGE_PROXY:-none}`. Before that, `ops/checks.py edge`, run by `deploy.sh`'s preflight and by `init.sh`, refuses any spelling outside the allowlist. A value never falls back silently.
3. **The connector runs in the stack, and only it may name a visitor** (the maintainer's choice, 2026-11-28, Q76).
   - `cloudflared` is an optional compose service that runs only with `EDGE_PROXY=cloudflare` (a compose profile, M13.3). It joins `tunnel`, an internal network on `EDGE_TUNNEL_SUBNET` that only Caddy and `cloudflared` join, with the fixed `ipv4_address` `EDGE_TUNNEL_ADDRESS`. It also joins a network of its own with a way out to Cloudflare. It shares no network with `web`, publishes no port, and is hardened like the rest of the stack (uid 1000, read-only root, `cap_drop: ALL`, `no-new-privileges`, memory and process limits).
   - Caddy, in that mode: `trusted_proxies static {$EDGE_TUNNEL_ADDRESS}/32` and `client_ip_headers Cf-Connecting-Ip`. The literal `/32` makes Caddy itself refuse a range, a list, a keyword such as `private_ranges`, and an empty value (measured), where `{$VAR}` alone accepts `0.0.0.0/0`. One key sets both the connector's address and Caddy's trust, so the two cannot disagree. `ops/checks.py edge` also refuses an address outside the subnet, the subnet's network and gateway addresses, and IPv6: under the literal `/32`, an IPv6 value would be a range of 2^96 addresses.
   - No other header is believed: not `X-Forwarded-For`, which Cloudflare appends to whatever the visitor sent, and not `True-Client-IP`.
4. **Caddy writes the one address Django reads, in every mode.** Both proxies get `header_up X-Forwarded-For {client_ip}` and `header_up -Cf-Connecting-Ip`.
   - Without a trusted proxy, `{client_ip}` is the peer, so nothing changes. Without the line, Caddy appends its peer to a trusted proxy's `X-Forwarded-For`, and Django, which believes the last entry, would see the connector.
   - The raw `CF-Connecting-IP` never reaches Django. From an untrusted peer, Caddy would otherwise pass it on as sent.
   - Django does not change: `ALLAUTH_TRUSTED_PROXY_COUNT = 1` and `client_ip` stay as they are.
   - Measured from the trusted address: the visitor is the left-most valid address among every `CF-Connecting-IP` value (two headers, or a list), IPv6 included and with any port removed. A value that is not an address leaves the connector's own. From any other address the header is ignored and the visitor is the peer.
5. **A certificate from files** (`EDGE_TLS=files`).
   - Caddy reads `tls /etc/caddy/tls/cert.pem /etc/caddy/tls/key.pem` in the site, with `auto_https disable_certs` and `default_sni {$APP_DOMAIN}` as global options. Nothing is requested from ACME in that mode.
   - `./tls` is a read-only bind mount. The key is mode 0400 and owned by the containers' uid 1000; under a rootless daemon that owner is set through [ADR-0033](0033-rootless-docker-host.md)'s helper.
   - `default_sni` serves the file's certificate to a client that sends no server name, which is what a browser does for an IP address.
   - With the tunnel, the certificate names the tunnel's host name and the connector verifies it, with an origin server name and a CA pool and never "No TLS Verify". ACME's challenges cannot reach Caddy for that name: HTTP-01 needs port 80, which the tunnel does not carry, and TLS-ALPN-01 needs the handshake that Cloudflare terminates. So `EDGE_PROXY=cloudflare` needs `EDGE_TLS=files`.
   - The CA pool names the issuer even for a Cloudflare Origin CA certificate. `cloudflared` adds Cloudflare's Origin CA roots to its pool today, but its source marks them for removal.
   - `ops/checks.py edge` refuses a certificate that does not name `APP_DOMAIN` and `EDGE_EXTRA_HOST`, or that has no key beside it, and warns 30 days before it expires.
6. **Cloudflare does not rewrite pages.** In `cloudflare` mode Caddy adds `Cache-Control: no-transform` to every response.
   - The header is deferred, so it is added after Caddy's own compression. Set earlier, it makes Caddy skip compression altogether (both measured).
   - Cloudflare's features that change a page's body honour it, according to its documentation: Rocket Loader, Email Address Obfuscation, JavaScript detections, the Web Analytics beacon, Automatic HTTPS Rewrites and the rest. Their scripts would load from the host's own `/cdn-cgi/` path, which `script-src 'self'` allows, so the CSP alone would not stop them.
   - The CSP does not change.
   - The header cannot stop a challenge, which replaces the page, or a cache rule that ignores the origin's `Cache-Control`. The runbook keeps both off for the host and checks that a signed-in page arrives with `cf-cache-status: DYNAMIC`.
7. **Cloudflare's credentials stay out of the logs, and the tunnel's token is a file.**
   - Caddy's two log filters delete `Cf-Access-Jwt-Assertion`, Access's token for the person, which carries their email. `Cookie`, where Access's `CF_Authorization` sits, stays redacted by Caddy's default.
   - The token paths are redacted on every host name, because the filters match paths, not hosts.
   - The tunnel's token reaches `cloudflared` only as a file (`TUNNEL_TOKEN_FILE`, cloudflared 2025.4 or later). It is a read-only mount owned and read like the TLS key, never an environment value, which `docker compose config` and `docker inspect` would print. Cloudflare itself moved its service installs from `--token` to `--token-file` in 2026.
8. **Cloudflare terminating TLS is exception X16** (the maintainer's decision, 2026-11-28, Q77). It holds for the pilot, with review by 2027-02-26 and again before v1.0.0 (H8.8). A VPS on ACME keeps TLS from the visitor to Caddy.
9. **Every path of the tunnel's host name stays behind Access, the token links included** (the maintainer's decision, 2026-11-28).
   - The connector also checks Access's token itself (the `access` origin setting: the team name and the application's AUD tag), so nothing that skipped Access reaches Caddy through the tunnel.
   - No bypass policy is used.
   - Each person is added to Access before their invitation is sent. During an email change, both addresses stay in Access until the change applies or is cancelled, because the old address's cancel link must still open.
10. **Caddy runs as uid 1000 with no capability** (M13.2).
    - It listens on 8080 and 8443 in its container. The production overlay publishes `${PUBLISHED_HTTP_PORT:-80}:8080` and `${PUBLISHED_HTTPS_PORT:-443}:8443` (TCP and UDP). ACME's challenges arrive on the published ports, and Caddy answers them on its own.
    - **`Alt-Svc` names the port visitors reach.** Measured on 2.11.9: Caddy advertises HTTP/3 on the port it listens on (`h3=":8443"`), where HTTP/3 clients would then go. The site rewrites that value to `ALT_SVC_PORT`: 443 in production, and locally the published port, which is also the one Caddy listens on. With HTTP/3 off there is no `Alt-Svc`, and the rewrite adds none. It applies to the gate's and the fallback's 503 too (measured). Links and redirects keep `HTTPS_PORT_SUFFIX`.
    - **Its volumes change owner through a one-shot `caddy-perms` service.** It runs before Caddy (`service_completed_successfully`) and uses the same pinned image, `network_mode: none`, a read-only root, and `cap_drop: ALL` plus `cap_add: [CHOWN, DAC_READ_SEARCH]`. It is uid 0 only to `chown -R 1000:1000 /data /config`. It also converts an existing installation at its first deploy, keeping its certificates and its internal CA.
      - `DAC_READ_SEARCH` is there because **`CHOWN` alone fails from the second run on** (measured under both daemons). Caddy makes its directories 0700, and a root without that capability cannot enter one it does not own, so `chown -R` exits 1 and Caddy would not start. Giving the volumes back to root (below) fails the same way.
      - It widens nothing `CHOWN` does not already give: whoever can take a file's ownership can then read it.
    - **`deploy.sh` and `recover.sh` run the one-shot themselves** (`start_caddy`, `ops/common.sh`). They start Caddy with `--no-deps`, which skips `depends_on`. At step 4 it runs behind the gates. A failed one-shot stops the script before Caddy is recreated.
    - **Caddy reads its bind-mounted files as uid 1000.** Under the rootless daemon that is a shifted uid, which only the "other" bits let in; container root, which Caddy was until then, is `deploy`, their owner. `init.sh` makes `caddy/` world-readable, and `deploy.sh` refuses at step 1 a file or directory of `caddy/` closed to "other". `flags/` was already searchable by the worker's same uid; `require_flags_searchable` now protects Caddy's gate too.
    - **Measured: no release before M13.2 can start Caddy on what uid 1000 wrote.** Those releases run Caddy as root with only `NET_BIND_SERVICE`, so it reads nothing in Caddy's 0600 files and 0700 directories, and exits. `recover.sh`'s rollbacks keep the checkout's compose files, so they keep the new Caddy and are unaffected. Deploying an older release from its own checkout needs the volumes given back to uid 0 first, with the same one-shot from the newer checkout (`chown -R 0:0`, [deploy-rollback.md](../runbooks/deploy-rollback.md#a-release-before-v040)). The rehearsal proves both directions: the old Caddy refuses the converted volumes, starts once they are given back, and the next deploy converts them again with the same CA root and site certificate.

## Alternatives rejected

- **`cloudflared` on the host, trusting the address it arrives from** (the pilot's setup, and Q76's working assumption). Under the rootless daemon a host connection through the loopback reaches Caddy as `10.0.2.2`, because slirp4netns translates 127.0.0.0/8 to it. Trusting that address trusts every local process, which could then name any visitor. Under a root daemon it would be the bridge's gateway, through which docker-proxy relays the host's connections. Refused by the maintainer on 2026-11-28 for the narrower trust of point 3.
- **Trusting the tunnel network's subnet.** Its gateway address belongs to the daemon's network namespace, which is the host's under a root daemon, so a process there could send from it. A fixed address cannot be taken that way.
- **A local Caddyfile or overlay** (today's pilot): nothing in the release keeps it working.
- **A derived Caddy image whose `/data` belongs to uid 1000.** Docker copies an image's ownership only into a new, empty volume, so existing installations would need the `chown` anyway. The image would also join the release pipeline: built, scanned and signed.
- **Django sending `no-transform`:** Caddy would then compress no page, in every mode (measured).
- **A CSP with a path or a nonce for scripts,** to refuse the injected `/cdn-cgi/` scripts. A path needs every host name in the policy, and a nonce goes on every script tag. `no-transform` stops the injection where it starts.
- **An Access bypass for the token paths:** they would face the internet as on a VPS, a stolen link would work for anyone, and the connector could not check Access's token on every request.

## Consequences

- **Trust widens from nobody to one container.** Whoever controls `cloudflared` (its image or its token) can name any visitor, and sees everything that passes, as Cloudflare does.
- **The token is as sensitive as the TLS key.** Whoever holds it can run another connector for the tunnel, and Cloudflare sends visitors to the nearest one. It is rotated with the other secrets after a compromise.
- **A third-party image joins the stack,** pinned by digest and scanned with the others (its exceptions go in X1), started only in this mode. Its network with a way out serves it alone.
- **Access protects the tunnel's host name only.** The LAN address and the host's loopback never meet it, and it is no authorization layer of the application.
- **IPv6 visitors arrive through Cloudflare even to a host without IPv6.** A per-address limit then counts one address of the /64 such a visitor holds, while the per-account limits still bound guessing. Grouping a /64 is backlog. Cloudflare's Pseudo IPv4 hashes the whole address, so it is no help and stays off.
- **CI cannot run a real tunnel,** which needs a Cloudflare account. M13.4 proves the trust with a stand-in container at the connector's address, and H13.6 checks the real tunnel by hand.
- **Other records change.** This amends ADR-0008 (Caddy believes one address in one mode, and runs as uid 1000) and plan §5's "Client IP" (Caddy remains the only proxy Django believes). The threat model's part *The edge* in `docs/security.md` follows this record.
