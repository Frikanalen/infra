# wireguard_server

A road-warrior WireGuard endpoint on `util1`, listening on `frikanalen.no`'s
public address and giving connected clients a route to `192.168.3.0/24`.
Nothing else -- no split-DNS, no internet routing through the tunnel, and
client provisioning is one helper script (`wg-add-client`), not automation.

## Addressing

- Clients dial `158.36.191.229:51820` (`wireguard_listen_ip`/`_listen_port`),
  the same address `roles/util_wan` puts on `util1`'s WAN interface.
- The tunnel itself is `192.168.5.0/24` (`wireguard_client_net`), server at
  `.1` (`wireguard_server_address`).
- pf lets `192.168.5.0/24` reach `192.168.3.0/24` and nothing further.
  `roles/util_fw`'s NAT only translates `192.168.3.0/24`, so a client default
  route pointed at the tunnel would still have nowhere to go once packets left
  `192.168.5.0/24` -- this is deliberately LAN access, not a default-route VPN.

## The server key

Generated once, on `util1`, the first time this role runs, and never touched
again -- `wg0.conf` is deployed with `force: false` for the same reason
`dns_server_zones_dynamic` uses it on zone files: this role does not own that
file's contents after the first write. There is nothing to put in
`data/vault.yml`; the private key never leaves the host it was generated on.

Run the playbook and it prints the server's public key. That, plus
`wireguard_listen_ip:wireguard_listen_port`, is everything a client config
needs on the peer side.

## Adding a client

On `util1`, as root:

```sh
wg-add-client alice > alice.conf
```

The role installs this script to `/usr/local/sbin/wg-add-client`. It
generates the client's keypair and a per-client preshared key, takes the next
free address in `192.168.5.0/24`, appends a `[Peer]` block (marked `# client:
alice (date)`) to `wg0.conf`, applies it to the running interface with `wg
syncconf` so other peers stay connected, and prints the client's complete
config to stdout. Hand `alice.conf` to the client over a secure channel and
delete it afterwards: it holds the client's private key, which is not stored
anywhere else. (The PSK is also kept in `wg0.conf`.)

Overrides, as environment variables: `WG_ENDPOINT` (default
`158.36.191.229:51820`), `WG_ALLOWED` (default `192.168.3.0/24`), `WG_DNS`
(default `192.168.3.2`, util1's BIND -- UniFi's importer rejects a config with no
`DNS` line, and wants a bare IP rather than a hostname), `WG_IF`, `WG_DIR`, `WG_NET_PREFIX`. The script's defaults
duplicate `wireguard_listen_ip`/`_listen_port` and `wireguard_client_net`, so
change them in `files/wg-add-client.sh` if you change those.

The address list is the `[Peer]` blocks in `wg0.conf` themselves; the
`# client:` comments say whose is whose. There is no registry elsewhere.

To do it by hand instead: `wg genkey`, `wg genpsk`, append a `[Peer]` with
`PublicKey`, `PresharedKey` and `AllowedIPs = 192.168.5.X/32` to `wg0.conf`,
then `wg syncconf wg0 <(wg-quick strip wg0)` (bash/zsh) or `service wireguard
restart`.

## Removing a client

Delete its `[Peer]` block from `wg0.conf` and restart the service. Nothing
elsewhere references client keys or addresses.
