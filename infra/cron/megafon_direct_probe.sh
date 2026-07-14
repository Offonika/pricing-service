#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  megafon_direct_probe.sh [--full] [--output-dir DIR] <url1> [url2 ...]
  megafon_direct_probe.sh [--full] [--output-dir DIR] --urls-file FILE

What it does:
  - shows direct egress IP without HTTP(S) proxy
  - resolves the Megafon host
  - checks TCP connectivity to port 443
  - tries a safe byte-range download by default
  - with --full downloads the whole file
EOF
}

FULL_DOWNLOAD=0
OUTPUT_DIR="${TMPDIR:-/tmp}/megafon-direct-probe"
URLS_FILE=""
URLS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --full)
      FULL_DOWNLOAD=1
      shift
      ;;
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --urls-file)
      URLS_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      URLS+=("$1")
      shift
      ;;
  esac
done

if [ -n "$URLS_FILE" ]; then
  while IFS= read -r line; do
    line="$(echo "$line" | xargs)"
    [ -n "$line" ] || continue
    URLS+=("$line")
  done < "$URLS_FILE"
fi

if [ "${#URLS[@]}" -eq 0 ]; then
  usage >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

probe_direct_ip() {
  local ip=""
  set +e
  ip="$(curl --noproxy '*' -fsS --max-time 8 https://api.ipify.org 2>/dev/null)"
  local status=$?
  set -e
  if [ "$status" -eq 0 ] && [ -n "$ip" ]; then
    echo "direct_egress_ip=$ip"
  else
    echo "direct_egress_ip=unknown"
  fi
}

probe_one() {
  local url="$1"
  local host
  host="$(python3 -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).hostname or "")' "$url")"
  local name
  name="$(python3 -c 'import os, sys; from urllib.parse import urlparse, unquote; print(os.path.basename(unquote(urlparse(sys.argv[1]).path)) or "download.bin")' "$url")"
  local out_path="$OUTPUT_DIR/$name"

  echo "=== $url ==="
  echo "host=$host"

  if [ -n "$host" ]; then
    echo "dns:"
    getent ahosts "$host" | head -n 3 || true
  fi

  if command -v nc >/dev/null 2>&1 && [ -n "$host" ]; then
    echo "tcp_443:"
    timeout 8 bash -lc "nc -vz '$host' 443" || true
  else
    echo "tcp_443: skipped (nc not found)"
  fi

  if [ "$FULL_DOWNLOAD" -eq 1 ]; then
    curl --noproxy '*' \
      -sS -L \
      --connect-timeout 8 \
      --max-time 120 \
      -o "$out_path" \
      -w 'result code=%{http_code} type=%{content_type} size=%{size_download} remote=%{remote_ip} path=%{filename_effective}\n' \
      "$url" || true
  else
    curl --noproxy '*' \
      -sS -L \
      --range 0-0 \
      --connect-timeout 8 \
      --max-time 30 \
      -o "$out_path.part" \
      -w 'result code=%{http_code} type=%{content_type} size=%{size_download} remote=%{remote_ip}\n' \
      "$url" || true
  fi

  echo
}

probe_direct_ip
for url in "${URLS[@]}"; do
  probe_one "$url"
done
