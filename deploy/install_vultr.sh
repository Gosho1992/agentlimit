#!/usr/bin/env bash
# One-shot install on a fresh Vultr Cloud Compute (VX1) Ubuntu 24.04 instance.
#
#   sudo DOMAIN=<your-ip-with-dashes>.sslip.io EMAIL=you@example.com bash deploy/install_vultr.sh
#
# DOMAIN is optional but recommended: with it, HTTPS is configured with a
# Let's Encrypt certificate. A free hostname that points at your VM's IP is
# <ip-with-dots-or-dashes>.sslip.io (e.g. 203-0-113-7.sslip.io).
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"

echo "== 1/8 host prerequisites"
[ "$(id -u)" -eq 0 ] || { echo "FAIL: run as root (sudo)"; exit 1; }
[ -e /dev/kvm ] || { echo "FAIL: /dev/kvm missing — use a VX1 instance with KVM"; exit 1; }
[ -f /sys/fs/cgroup/cgroup.controllers ] || { echo "FAIL: cgroup v2 not mounted"; exit 1; }
echo "kvm ok; cgroup v2 controllers: $(cat /sys/fs/cgroup/cgroup.controllers)"

echo "== 2/8 packages"
apt-get update -y
apt-get install -y python3-venv python3-pip nginx curl ca-certificates openssl
if ! command -v node >/dev/null || [ "$(node -v | cut -c2- | cut -d. -f1)" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi

echo "== 3/8 python env"
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r backend/requirements.txt

echo "== 4/8 config, access token, demo data"
[ -f backend/.env ] || cp backend/.env.example backend/.env
chmod 600 backend/.env
if ! grep -q '^ACCESS_TOKEN=.\+' backend/.env; then
  TOKEN="$(openssl rand -hex 16)"
  sed -i "s/^ACCESS_TOKEN=.*/ACCESS_TOKEN=${TOKEN}/" backend/.env
  echo "Generated ACCESS_TOKEN=${TOKEN}  (give this to judges in the submission)"
fi
mkdir -p /var/lib/agentlimit/ledger /var/lib/agentlimit/keys
chmod 700 /var/lib/agentlimit/keys
venv/bin/python -m backend.scripts.seed_demo_data

echo "== 5/8 dashboard build"
(cd frontend && npm ci --no-audit --no-fund && npm run build)

echo "== 6/8 systemd + nginx"
sed "s#__REPO__#${REPO}#g" deploy/agentlimit.service > /etc/systemd/system/agentlimit.service
sed "s#__SERVER_NAME__#${DOMAIN:-_}#g" deploy/nginx.conf > /etc/nginx/sites-available/agentlimit
ln -sf /etc/nginx/sites-available/agentlimit /etc/nginx/sites-enabled/agentlimit
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
if command -v ufw >/dev/null; then ufw allow 22/tcp || true; ufw allow 80/tcp || true; ufw allow 443/tcp || true; fi

echo "== 7/8 HTTPS"
if [ -n "$DOMAIN" ]; then
  apt-get install -y certbot python3-certbot-nginx
  if [ -n "$EMAIL" ]; then MAILOPT=(-m "$EMAIL"); else MAILOPT=(--register-unsafely-without-email); fi
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect "${MAILOPT[@]}"
  sed -i "s/^COOKIE_SECURE=.*/COOKIE_SECURE=true/" backend/.env
  echo "HTTPS enabled: https://${DOMAIN}/"
else
  echo "No DOMAIN given: serving HTTP only. Do not describe the deployment as HTTPS."
fi
systemctl daemon-reload
systemctl enable agentlimit
systemctl restart agentlimit

echo "== 8/8 infrastructure gate"
venv/bin/python -m backend.scripts.day1_gate | tee /var/lib/agentlimit/day1_gate.txt || true
echo
echo "Next: set VULTR_INFERENCE_KEY (and VULTR_MODEL_* IDs) in backend/.env, then: systemctl restart agentlimit"
echo "Readiness: curl -s -H \"Authorization: Bearer \$(grep ^ACCESS_TOKEN backend/.env | cut -d= -f2)\" http://127.0.0.1:8000/health"
