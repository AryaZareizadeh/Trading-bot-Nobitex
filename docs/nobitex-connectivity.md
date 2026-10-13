# Nobitex connectivity in Docker

Nobitex requests must use a direct application connection: no explicit proxy and no inherited `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY`. Other exchanges use the configured proxy/VPN route. Do not clear those generic variables globally to repair Nobitex.

Python can bypass an application proxy with `session.trust_env = False` and an empty proxy mapping. It cannot bypass the host's network routing. If a full-tunnel VPN captures all traffic, configure Nobitex domain/destination split tunneling in that VPN or the host/router, while keeping other exchanges on their intended route. Do not hardcode exchange IPs; addresses can change. [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/)

## Current v2rayN and other-exchange routing

Read-only checks on 2026-10-06 identified v2rayN **7.24.9** with Xray, **TUN disabled**, and Windows system proxy enabled at **127.0.0.1:10808**. The Xray listener is bound only to loopback; v2rayN's LAN listener is disabled. Docker Desktop's stored `ContainersProxyHTTPMode` is `system`, which uses the host's configured proxy. [Docker proxy settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/#proxies)

In this mode, the bot's Nobitex requests bypass the application/system proxy. No v2rayN routing change is needed for the bot while TUN remains disabled. Requests from native Windows applications that permit environment/system proxy discovery may use the Windows registry proxy even without `HTTP_PROXY` or `HTTPS_PROXY`; the Nobitex sessions disable that discovery. [Python proxy discovery](https://docs.python.org/3/library/urllib.request.html#urllib.request.getproxies)

The dashboard's `docker.env` configures other exchanges through Docker Desktop's reachable internal proxy:

```dotenv
HTTP_PROXY=http://gateway.docker.internal:3128
HTTPS_PROXY=http://gateway.docker.internal:3128
NO_PROXY=localhost,127.0.0.1,::1,nobitex.ir,.nobitex.ir,nobitx.com,.nobitx.com
```

The HTTP URL is also used for `HTTPS_PROXY`: HTTPS requests use an HTTP CONNECT tunnel through that proxy and retain TLS verification. Nobitex sessions ignore these proxy variables; `NO_PROXY` supplies an additional exception for tools that honor it. Leave `NOBITEX_HTTPS_PROXY` unset. [Requests proxies](https://requests.readthedocs.io/en/latest/user/advanced/#proxies)

Public Binance, KuCoin, and Bitfinex checks through `gateway.docker.internal:3128` returned HTTP 200 during troubleshooting. That confirms those public requests used a reachable proxy; it does not validate authenticated exchange operations. Use this verified Docker endpoint rather than `host.docker.internal:10808`: the observed v2rayN listener does not accept LAN connections. No change to v2rayN's LAN exposure is needed.

After recreating the service, these public checks exercise the container's inherited proxy configuration without exposing environment values or credentials:

```powershell
@'
import requests

urls = {
    "binance": "https://data-api.binance.vision/api/v3/ping",
    "kucoin": "https://api.kucoin.com/api/v1/timestamp",
    "bitfinex": "https://api-pub.bitfinex.com/v2/platform/status",
}
with requests.Session() as session:
    session.trust_env = True
    for label, url in urls.items():
        try:
            with session.get(url, timeout=(5, 10), stream=True, allow_redirects=False) as response:
                print(f"{label}_proxy: HTTP {response.status_code}", flush=True)
        except Exception as exc:
            print(f"{label}_proxy: {type(exc).__name__}", flush=True)
'@ | docker compose exec -T dashboard python -
```

## If v2rayN TUN is enabled later

TUN can capture direct application sockets. To keep Nobitex traffic off the remote VPN node, add a domain rule to the **active** v2rayN rule set before enabling TUN. Preserve the other rules and their final proxy route.

In v2rayN 7.24.9, open **Settings → Routing Setting**, double-click the active rule set, then choose **Add Rule**. Enable the rule, set `outboundTag` to **direct**, and enter this in **Domain**:

```text
domain:nobitex.ir,domain:nobitx.com
```

Set **Rule Type** to **Routing**; leave IP, process, port, protocol, inbound tag, and network restrictions empty. Confirm the rule, select it in **Rule List**, choose **Move to top**, then confirm the rule set. Ensure the same rule set remains active and reload the core. These English control names and fields come from the installed version's official source. [Rule editor](https://github.com/2dust/v2rayN/blob/7.24.9/v2rayN/v2rayN/Views/RoutingRuleDetailsWindow.xaml), [rule list](https://github.com/2dust/v2rayN/blob/7.24.9/v2rayN/v2rayN/Views/RoutingRuleSettingWindow.xaml), [rule types](https://github.com/2dust/v2rayN/blob/7.24.9/v2rayN/ServiceLib/Enums/ERuleType.cs)

`domain:nobitex.ir` matches that domain and every subdomain, including production/testnet API hosts; `domain:nobitx.com` covers the help site. Do not enter literal `*.nobitex.ir` in an Xray domain rule or use a plain keyword. Rules are evaluated from top to bottom and the first matching rule wins, so a domain rule below a catch-all proxy rule cannot take effect. [Xray routing](https://xtls.github.io/en/config/routing)

If v2rayN uses sing-box later, its rule converter maps this same `domain:` syntax to `domain_suffix`. Keep sing-box in **Rule** mode: its global-proxy mode takes precedence over user rules. [v2rayN sing-box rule conversion](https://github.com/2dust/v2rayN/blob/7.24.9/v2rayN/ServiceLib/Services/CoreConfig/Singbox/SingboxRoutingService.cs), [sing-box domain rules](https://sing-box.sagernet.org/configuration/route/rule/)

Domain routing requires the core to know the destination hostname, through proxy addressing, DNS mapping, or TLS sniffing. Docker resolves names through its own network path, so repeat the direct container HTTPS check after any TUN change and verify the selected outbound in v2rayN's connection logs. HTTP 200 alone proves reachability, not which VPN outbound was used. Keep direct DNS reachable on the current network; system-proxy bypass exceptions do not themselves bypass TUN. [Xray hostname routing and sniffing](https://xtls.github.io/en/config/routing), [v2rayN TUN/DNS settings](https://github.com/2dust/v2rayN/wiki/Description-of-some-ui)

## Check Docker and the container

Run from PowerShell in the repository directory:

```powershell
docker version --format '{{.Server.Version}}'
docker compose ps dashboard
```

The Compose service is `dashboard`. If a command fails with a named-pipe permission error, resolve Docker Desktop/engine access in that terminal first. That error alone says nothing about Nobitex DNS or TLS.

The earlier application used an explicit `NOBITEX_HTTPS_PROXY` for wallet/order requests while candles and display quotes used another route. A working chart did not establish a working wallet route. The corrected policy applies the same direct connection to all Nobitex REST requests; the old Nobitex-specific proxy override must be absent from `docker.env` and is ignored by the application. Generic proxy variables used by other exchanges remain intact.

## Test public DNS, TCP, and TLS from the container

These checks do not load exchange credentials or place orders. Each probe has an independent deadline; output contains only labels, address families, HTTP status, elapsed time, and exception classes.

```powershell
$nobitexProbe = @'
import queue
import socket
import threading
import time

import requests

HOST = "apiv2.nobitex.ir"
URL = "https://apiv2.nobitex.ir/v3/orderbook/BTCUSDT"

def check(label, operation, deadline=20):
    output = queue.Queue()
    started = time.monotonic()
    def worker():
        try:
            output.put(("ok", operation()))
        except Exception as exc:
            output.put(("failed", type(exc).__name__))
    threading.Thread(target=worker, daemon=True).start()
    try:
        state, detail = output.get(timeout=deadline)
        print(f"{label}: {state}, {detail}, {time.monotonic()-started:.1f}s", flush=True)
    except queue.Empty:
        print(f"{label}: deadline exceeded ({deadline}s)", flush=True)

def dns():
    records = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
    families = sorted({"IPv4" if row[0] == socket.AF_INET else "IPv6" for row in records})
    return "/".join(families)

def tcp():
    with socket.create_connection((HOST, 443), timeout=5):
        return "connected"

def public_get():
    with requests.Session() as session:
        session.trust_env = False
        session.proxies.clear()
        with session.get(URL, timeout=(5, 10), stream=True, allow_redirects=False) as response:
            return f"HTTP {response.status_code}; TLS verification enabled"

check("nobitex_dns", dns, 6)
check("nobitex_tcp_443", tcp, 8)
check("direct_public_https", public_get)
'@
$nobitexProbe | docker compose exec -T dashboard python -
```

`HTTP 200` proves this public route completed resolution, connection, TLS verification, and HTTP. An HTTP error also shows an HTTP responder was reached, but requires a separate exchange/network-policy check. These probes do not validate a private account key, its IP whitelist, or trade permissions. Respect any rate-limit backoff before repeating. [Compose exec](https://docs.docker.com/reference/cli/docker/compose/exec/)

If the configured exchange is testnet, repeat with `HOST = "testnetapiv2.nobitex.ir"` and the corresponding testnet URL. A production-host success does not establish testnet reachability.

## Compare the host's direct route

Windows includes `curl.exe`; the command below disables application proxy use and retains certificate verification:

```powershell
curl.exe --noproxy '*' --connect-timeout 5 --max-time 20 --silent --show-error --output NUL --write-out 'HTTP %{http_code}\n' 'https://apiv2.nobitex.ir/v3/orderbook/BTCUSDT'
```

| Result | Next step |
|---|---|
| Container DNS fails or exceeds its deadline | Compare with host resolution. Check Docker Desktop's current network/DNS settings and the ISP/VPN resolver. Use DNS reachable on this network; do not assume an external public DNS server works in Tehran. |
| DNS works; TCP or HTTPS times out | Check direct outbound routing, VPN split tunneling, and the host firewall's treatment of Docker Desktop. A browser succeeding through its own proxy does not establish a direct route. |
| Host direct succeeds; container direct fails | Investigate Docker Desktop/VM DNS, VPN handling of Docker traffic, and host firewall rules. Compare IPv4/IPv6 results before changing network-family settings. |
| `SSLError` | Investigate the certificate chain, hostname, system clock, or network interception. Keep TLS verification enabled. |
| Both direct HTTPS checks succeed | The public transport is reachable at test time. Check the application's current image, account permissions/IP whitelist, server response/backoff, and durable order error separately. |
| HTTP 429 | Honor the reported backoff. Repeating probes aggressively does not repair a rate-limit failure. |

Do not replace the Nobitex hostname with a guessed IP or disable TLS verification. Docker Desktop has network-family/DNS filtering controls; choose settings supported by actual results rather than changing them blindly. [Docker DNS and network settings](https://docs.docker.com/desktop/features/networking/networking-how-tos/#network-how-tos-for-mac-and-windows)

## Apply the direct-only policy

Remove the `NOBITEX_HTTPS_PROXY` line from the existing private `docker.env`, preserving every other setting. Configure the generic proxy variables shown above for non-Nobitex services. The local original environment was backed up before the change under `.runtime/nobitex-routing-backup-20261006/docker.env`; that private backup must not be committed or shared.

Environment changes require recreating the service; `docker compose restart` retains the old container environment. This project copies application source into the image, so deploying the routing fix requires rebuilding it. [Compose restart](https://docs.docker.com/reference/cli/docker/compose/restart/), [Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)

```powershell
docker compose up --build -d --force-recreate dashboard
docker compose ps dashboard
$nobitexProbe | docker compose exec -T dashboard python -
```

Use the same Compose file combination used to deploy the service, including any CUDA override. Recreation interrupts the dashboard but preserves its named data volume. Inspect saved positions and unresolved orders before starting real bots. Do not use `down -v` as a connectivity repair.

On 2026-10-06, troubleshooting confirmed DNS resolution and HTTP 200 from direct public orderbook checks on both the host and container while the host VPN was active. This verifies reachability at that time; it does not prove that a full-tunnel VPN excludes Nobitex or that authenticated trading requests will succeed.

The direct-only image and generic other-exchange proxy settings were deployed on
2026-10-06 after approval. The recreated dashboard is healthy. Public checks in
the deployed container returned HTTP 200 for Nobitex through the direct session
and Binance, KuCoin, and Bitfinex through the inherited Docker proxy. The local
health endpoint also returned HTTP 200 with proxy bypass. No authenticated
exchange requests were used for verification, and bots were not restarted.

A verified runtime bundle and the original private environment file were retained
in the ignored `.runtime/nobitex-routing-backup-20261006` directory. Offline
verification reports 799 passing Python tests and the same 18 pre-existing
optimizer/research failures: missing local Optuna, optimizer response expectations,
and research metadata. All routing and candle-network recovery tests pass.

