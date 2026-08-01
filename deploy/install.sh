#!/usr/bin/env bash
# Instala el panel en el droplet. Idempotente: se puede volver a lanzar.
#
#   curl -fsSL .../install.sh | bash      NO. Clona el repo y lee esto antes.
#
#   git clone https://github.com/manu97galicia-maker/Agalaz-Bank.git /root/sniper-deck
#   cd /root/sniper-deck && bash deploy/install.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/root/sniper-deck}"
# Vacio a proposito: el panel busca el repo del bot solo (ai-bot, Definitivo_bot,
# ...). Solo se fija en el .env si aqui se pasa una ruta a mano.
BOT_REPO="${BOT_REPO:-}"

cd "$APP_DIR"

echo "==> Dependencias del sistema"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip

echo "==> Entorno virtual"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
# solders = firma de la wallet caliente; webauthn = login por cara/huella.
# Ambos son import perezoso: si una rueda falla en este Ubuntu, el resto del
# panel sigue funcionando (solo se cae esa función concreta).
./.venv/bin/pip install --quiet aiohttp PyYAML anthropic solders webauthn

if [ ! -f .env ]; then
  echo "==> Creando .env desde la plantilla"
  cp .env.example .env
  # Sin ruta explicita se borra la linea: mejor que el panel busque a que se
  # quede leyendo una carpeta que no es la del bot que corre.
  if [ -n "$BOT_REPO" ]; then
    sed -i "s#^BOT_REPO=.*#BOT_REPO=${BOT_REPO}#" .env
  else
    sed -i "s#^BOT_REPO=.*#\# BOT_REPO=   (vacio: el panel lo busca solo)#" .env
  fi
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

RESOLVED_REPO="$(./.venv/bin/python -c 'from server import config; print(config.BOT_REPO)' 2>/dev/null || true)"
if [ -n "$RESOLVED_REPO" ] && [ -d "$RESOLVED_REPO" ]; then
  echo "==> Repo del bot: $RESOLVED_REPO"
else
  echo "    !! Aviso: no encuentro el repo del bot (probé ai-bot, Definitivo_bot...)."
  echo "       Pásale la ruta buena: BOT_REPO=/root/ai-bot bash deploy/install.sh"
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
