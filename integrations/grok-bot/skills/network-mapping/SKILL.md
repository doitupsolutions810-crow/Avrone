---
name: network-mapping
description: Map how far your environment reaches on the network, safely, using only your own host and public endpoints.
---
1. Own interfaces: shell `ip -j addr` (env_install iproute2 via apt if missing).
2. Egress: ip_geo with no host. Then rdap_whois on the egress IP.
3. Reachability: tcp_ping 1.1.1.1:443 and github.com:443; dns_lookup a couple of names.
4. Path: traceroute to one public endpoint (env_install traceroute or mtr-tiny if missing).
5. Never sweep ports or probe private ranges you do not own. One host and port per call.
6. Report where you sit, farthest point reached, each hop, and the walls (with the exact error).
