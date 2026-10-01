#!/bin/sh
# Add a WireGuard client on util1 and print its ready-to-use config.
#
#   wg-add-client.sh <name> > <name>.conf
#
# Generates the client keypair and a per-client PSK, picks the next free
# address in the tunnel network, appends a [Peer] block to wg0.conf, applies
# it to the running interface without dropping other peers, and writes the
# client's complete config to stdout. Everything else goes to stderr, so the
# redirect above captures only the config.
#
# The client's private key and the PSK exist only in that output (the PSK
# also in wg0.conf) -- nothing else is kept. Run as root; the output is a
# secret, so mind where it ends up.
#
# POSIX sh: util1 is FreeBSD, no bash assumed.

set -eu
umask 077

WG_IF="${WG_IF:-wg0}"
WG_DIR="${WG_DIR:-/usr/local/etc/wireguard}"
WG_ENDPOINT="${WG_ENDPOINT:-158.36.191.229:51820}"
WG_NET_PREFIX="${WG_NET_PREFIX:-192.168.5}"  # clients get <prefix>.2-254
WG_ALLOWED="${WG_ALLOWED:-192.168.3.0/24}"   # what the client routes via the tunnel
WG_DNS="${WG_DNS:-192.168.3.2}"              # util1's BIND; UniFi's importer requires a DNS IP

conf="$WG_DIR/$WG_IF.conf"

die() { echo "error: $*" >&2; exit 1; }

[ $# -eq 1 ] || die "usage: $0 <client-name>"
name="$1"
case "$name" in
    *[!A-Za-z0-9._-]* | '') die "client name may only contain letters, digits, '.', '_' and '-'" ;;
esac

[ "$(id -u)" -eq 0 ] || die "must run as root"
[ -f "$conf" ] || die "$conf not found -- has the wireguard_server role run?"
[ -f "$WG_DIR/$WG_IF.key" ] || die "$WG_DIR/$WG_IF.key not found"

if grep -q "^# client: $name\$" "$conf"; then
    die "client '$name' already exists in $conf"
fi

# Next free address: one past the highest .N already in AllowedIPs.
last=$(sed -n "s#^AllowedIPs *= *${WG_NET_PREFIX}\.\([0-9][0-9]*\)/32.*#\1#p" "$conf" | sort -n | tail -n 1)
n=$(( ${last:-1} + 1 ))
[ "$n" -le 254 ] || die "no free addresses left in ${WG_NET_PREFIX}.0/24"
addr="${WG_NET_PREFIX}.${n}"

client_key=$(wg genkey)
client_pub=$(printf '%s' "$client_key" | wg pubkey)
psk=$(wg genpsk)
server_pub=$(wg pubkey < "$WG_DIR/$WG_IF.key")

# Append to the server config first; only apply if that worked.
cat >> "$conf" <<EOF

# client: $name ($(date +%Y-%m-%d))
[Peer]
PublicKey = $client_pub
PresharedKey = $psk
AllowedIPs = $addr/32
EOF

# wg-quick strip drops the Address/etc. lines wg itself rejects. If the
# interface is not up (service stopped) the config is still saved and will
# be picked up on next start.
if ifconfig "$WG_IF" >/dev/null 2>&1; then
    wg-quick strip "$WG_IF" | wg syncconf "$WG_IF" /dev/stdin
    echo "applied to running $WG_IF" >&2
else
    echo "warning: $WG_IF is not up; peer saved, start with 'service wireguard start'" >&2
fi

echo "added '$name' as $addr" >&2

cat <<EOF
[Interface]
PrivateKey = $client_key
Address = $addr/32
DNS = $WG_DNS

[Peer]
PublicKey = $server_pub
PresharedKey = $psk
Endpoint = $WG_ENDPOINT
AllowedIPs = $WG_ALLOWED
PersistentKeepalive = 25
EOF
