#!/usr/bin/env bash
# Cierra el panel: HTTPS de verdad y el puerto 8080 fuera del alcance de internet.
#
# Antes de esto, el tramo Vercel -> droplet iba en texto plano y cualquiera podia
# escanear la IP y llamar directamente a la API. Despues:
#
#   movil --HTTPS--> Vercel --HTTPS--> Caddy --127.0.0.1:8080--> panel
#                                        |
#                          (8080 ya no existe de cara a fuera)
#
# Uso:
#   cd /root/sniper-deck
#   API_HOST=api-banco.tudominio.com bash deploy/harden.sh
#
# API_HOST es solo el extremo cifrado; tu sigues abriendo la URL de Vercel.
# Tiene que ser un NOMBRE con su DNS apuntando a este droplet: Let's Encrypt no
# firma certificados para una IP.
set -euo pipefail

APP_DIR="${APP_DIR:-/root/sniper-deck}"
API_HOST="${API_HOST:-}"

if [ -z "$API_HOST" ]; then
  cat >&2 <<'EOF'
Falta API_HOST.

  API_HOST=api-banco.tudominio.com bash deploy/harden.sh

Opciones:
  - Un subdominio tuyo con un registro A -> 159.89.19.12  (lo fiable)
  - 159-89-19-12.sslip.io                                  (sin tocar DNS;
    depende de un tercero y puede chocar con los limites de Let's Encrypt)
EOF
  exit 1
fi

cd "$APP_DIR"

echo "==> Comprobando que $API_HOST apunta aqui"
MY_IP="$(curl -fsS --max-time 10 https://api.ipify.org || echo '?')"
HOST_IP="$(getent hosts "$API_HOST" | awk '{print $1}' | head -1 || true)"
if [ -z "$HOST_IP" ]; then
  echo "    !! $API_HOST no resuelve todavia. Crea el registro A y espera unos minutos." >&2
  exit 1
fi
if [ "$HOST_IP" != "$MY_IP" ]; then
  echo "    !! $API_HOST resuelve a $HOST_IP pero este droplet es $MY_IP." >&2
  echo "       Caddy no podra sacar el certificado hasta que coincidan." >&2
  exit 1
fi
echo "    ok: $API_HOST -> $HOST_IP"

echo "==> El panel pasa a escuchar solo en loopback"
touch .env
if grep -q '^PANEL_HOST=' .env; then
  sed -i 's#^PANEL_HOST=.*#PANEL_HOST=127.0.0.1#' .env
else
  echo 'PANEL_HOST=127.0.0.1' >> .env
fi
# El navegador entra por HTTPS (Vercel), asi que la cookie debe ir con Secure.
if grep -q '^PANEL_COOKIE_SECURE=' .env; then
  sed -i 's#^PANEL_COOKIE_SECURE=.*#PANEL_COOKIE_SECURE=true#' .env
else
  echo 'PANEL_COOKIE_SECURE=true' >> .env
fi
chmod 600 .env

echo "==> Instalando Caddy"
if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi

echo "==> Configurando Caddy para $API_HOST"
sed "s#^API_HOST#${API_HOST}#" deploy/Caddyfile > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable --now caddy
systemctl reload caddy

echo "==> Cortafuegos: fuera solo 22, 80 y 443"
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp   >/dev/null 2>&1 || true
  ufw allow 80/tcp   >/dev/null 2>&1 || true
  ufw allow 443/tcp  >/dev/null 2>&1 || true
  ufw delete allow 8080/tcp >/dev/null 2>&1 || true
  ufw deny 8080/tcp  >/dev/null 2>&1 || true
  ufw --force enable >/dev/null 2>&1 || true
  ufw status numbered | sed 's/^/    /'
else
  echo "    !! ufw no esta instalado. Cierra el 8080 en el firewall de DigitalOcean." >&2
fi

echo "==> Reiniciando el panel"
systemctl restart sniper-deck
sleep 2
systemctl is-active sniper-deck | sed 's/^/    panel: /'

echo "==> Comprobaciones"
echo -n "    HTTPS (debe dar {\"ok\": true}): "
curl -fsS --max-time 15 "https://${API_HOST}/api/ping" || echo "FALLO"
echo
echo -n "    8080 desde fuera (debe fallar):  "
if curl -fsS --max-time 5 "http://${MY_IP}:8080/api/ping" >/dev/null 2>&1; then
  echo "!! SIGUE ABIERTO -- ciérralo en el firewall de DigitalOcean"
else
  echo "cerrado, bien"
fi

cat <<EOF

Hecho. Ahora actualiza vercel.json en el repo para que apunte al extremo cifrado:

  { "source": "/api/(.*)", "destination": "https://${API_HOST}/api/\$1" }

git add vercel.json && git commit -m "Vercel proxya por HTTPS" && git push
(Vercel redespliega solo al hacer push.)

Con eso ya puedes poner PANEL_HOT_WALLET=true y operar.
EOF
