#!/usr/bin/env bash
# Instala el panel en el droplet. Idempotente: se puede volver a lanzar.
#
#   curl -fsSL .../install.sh | bash      NO. Clona el repo y lee esto antes.
#
#   git clone https://github.com/manu97galicia-maker/WEB-APP-DATOS-FRANC-.git /root/sniper-deck
#   cd /root/sniper-deck && bash deploy/install.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/root/sniper-deck}"
BOT_REPO="${BOT_REPO:-/root/Definitivo_bot}"

cd "$APP_DIR"

echo "==> Dependencias del sistema"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip

echo "==> Entorno virtual"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
# solders = firma de la wallet caliente (compra/venta/envío). Si falla la rueda
# en este Ubuntu, el resto del panel sigue funcionando (import perezoso).
./.venv/bin/pip install --quiet aiohttp PyYAML anthropic solders

if [ ! -f .env ]; then
  echo "==> Creando .env desde la plantilla"
  cp .env.example .env
  sed -i "s#^BOT_REPO=.*#BOT_REPO=${BOT_REPO}#" .env
  sed -i "s#^PANEL_SESSION_SECRET=.*#PANEL_SESSION_SECRET=$(openssl rand -hex 32)#" .env
  chmod 600 .env
  echo
  echo "    !! Falta la contraseña. Ejecuta ahora:"
  echo "       cd $APP_DIR && ./.venv/bin/python -m server.hashpw"
  echo "       y pega PANEL_PASSWORD_HASH= en .env (y pon PANEL_USER)."
  echo
else
  echo "==> .env ya existe, no lo toco"
fi

if [ ! -d "$BOT_REPO" ]; then
  echo "    !! Aviso: no encuentro el repo del bot en $BOT_REPO."
  echo "       Ajusta BOT_REPO en .env o el panel no verá nada."
fi

echo "==> Servicio systemd"
install -m 644 deploy/sniper-deck.service /etc/systemd/system/sniper-deck.service
sed -i "s#/root/sniper-deck#${APP_DIR}#g" /etc/systemd/system/sniper-deck.service
systemctl daemon-reload
systemctl enable sniper-deck.service

echo "==> Cortafuegos: el panel NO se expone directo"
if command -v ufw >/dev/null 2>&1; then
  ufw allow 80/tcp  >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
  ufw deny 8080/tcp >/dev/null 2>&1 || true
fi

cat <<EOF

Listo. Siguiente paso:

  1) Si aún no lo has hecho:   ./.venv/bin/python -m server.hashpw
     y rellena PANEL_USER + PANEL_PASSWORD_HASH en $APP_DIR/.env
  2) Arranca:                  systemctl start sniper-deck
  3) Comprueba:                systemctl status sniper-deck --no-pager
  4) Publica con HTTPS:        copia deploy/Caddyfile a /etc/caddy/Caddyfile
                               (cambia el dominio) y  systemctl reload caddy
     Sin dominio, túnel SSH:   ssh -L 8080:127.0.0.1:8080 root@159.89.19.12
                               y abre http://127.0.0.1:8080
                               (en ese caso pon PANEL_COOKIE_SECURE=false)
EOF
