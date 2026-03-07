#!/bin/bash
# Fix DNS order to prioritize Tailscale MagicDNS
# Docker sometimes regenerates resolv.conf with wrong nameserver order,
# causing Tailscale hostname resolution to fail.

RESOLV_CONF="/etc/resolv.conf"
TAILSCALE_DNS="100.100.100.100"

if ! grep -q "nameserver $TAILSCALE_DNS" "$RESOLV_CONF"; then
    echo "[DNS Fix] Tailscale MagicDNS not in resolv.conf, skipping"
    exit 0
fi

if head -n 5 "$RESOLV_CONF" | grep -m1 "^nameserver" | grep -q "$TAILSCALE_DNS"; then
    echo "[DNS Fix] Tailscale MagicDNS already first, no change needed"
    exit 0
fi

echo "[DNS Fix] Reordering nameservers to prioritize Tailscale MagicDNS..."

HEADER_LINES=$(grep -v "^nameserver" "$RESOLV_CONF" || true)
OTHER_NAMESERVERS=$(grep "^nameserver" "$RESOLV_CONF" | grep -v "$TAILSCALE_DNS" || true)

{
    echo "$HEADER_LINES"
    echo "nameserver $TAILSCALE_DNS"
    echo "$OTHER_NAMESERVERS"
} | grep -v '^$' | sudo tee "$RESOLV_CONF" > /dev/null

echo "[DNS Fix] Complete: Tailscale MagicDNS (100.100.100.100) now first"
