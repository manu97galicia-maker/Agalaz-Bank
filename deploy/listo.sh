#!/usr/bin/env bash
# Deja el panel montado, con login que funciona y cifrado de punta a punta.
# Un solo comando, idempotente: se puede volver a lanzar sin romper nada.
#
#   cd /root/sniper-deck && bash deploy/listo.sh
#
# Variables opcionales:
#   API_HOST   nombre del extremo cifrado. Por defecto usa sslip.io, que resuelve
#              a la IP de este droplet sin tocar ningun DNS. Si tienes dominio
#              propio, pasalo:  API_HOST=api-banco.tudominio.com bash deploy/listo.sh
#   APP_DIR    donde vive el panel (por defecto /root/sniper-deck)
#   BOT_REPO   donde vive el bot   (por defecto /root/Definitivo_bot)
set -uo pipefail   # sin -e: queremos llegar al resumen final aunque algo falle

APP_DIR="${APP_DIR:-/root/sniper-deck}"
BOT_REPO="${BOT_REPO:-/root/Definitivo_bot}"

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
warn() { printf '  \033[33mAVISO\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFALLO\033[0m %s\n' "$1"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

FAILED=0
HTTPS_OK=0

cd "$APP_DIR" || { bad "No existe $APP_DIR. Clona el repo primero."; exit 1; }

# --------------------------------------------------------------------------- #
step "1/7  Codigo al dia"
# --------------------------------------------------------------------------- #
git pull --ff-only 2>&1 | sed 's/^/       /' && ok "repo actualizado" || warn "no pude hacer git pull; sigo con lo que hay"

# --------------------------------------------------------------------------- #
step "2/7  Dependencias"
# --------------------------------------------------------------------------- #
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
if ./.venv/bin/pip install --quiet aiohttp PyYAML anthropic solders; then
  ok "paquetes instalados (incluido solders para firmar)"
else
  bad "fallo instalando dependencias"; FAILED=1
fi

# --------------------------------------------------------------------------- #
step "3/7  Configuracion"
# --------------------------------------------------------------------------- #
[ -f .env ] || cp .env.example .env
chmod 600 .env

set_env() {  # set_env CLAVE valor
  if grep -q "^$1=" .env; then
    sed -i "s#^$1=.*#$1=$2#" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

grep -q '^PANEL_SESSION_SECRET=.\+' .env || set_env PANEL_SESSION_SECRET "$(openssl rand -hex 32)"
set_env BOT_REPO "$BOT_REPO"
set_env PANEL_HOST 127.0.0.1        # solo loopback: a 8080 no se llega desde fuera
set_env PANEL_COOKIE_SECURE true    # el navegador entra por HTTPS
ok "credenciales de sesion y rutas puestas"

[ -d "$BOT_REPO" ] && ok "bot encontrado en $BOT_REPO" \
                   || warn "no veo el bot en $BOT_REPO (ajusta BOT_REPO en .env)"

# La wallet caliente solo se enciende cuando el cifrado este verificado (paso 7).
set_env PANEL_HOT_WALLET false

# --------------------------------------------------------------------------- #
step "4/7  Usuarios (esto es lo que arregla el login)"
# --------------------------------------------------------------------------- #
# Si no hay contrasena y hay a alguien delante, se pide aqui en vez de mandarle a
# editar un fichero y relanzar. Se lee sin eco y va directa al .env: nunca pasa
# por la linea de comandos, que quedaria en el historial del shell.
if ! grep -q '^PANEL_SEED_PASSWORD=.\+' .env && [ -t 0 ]; then
  echo "       No hay contrasena configurada todavia."
  while :; do
    read -rsp "       Contrasena para entrar al panel: " PW1; echo
    if [ "${#PW1}" -lt 8 ]; then
      echo "       Demasiado corta (minimo 8). Esto guarda un wallet."
      continue
    fi
    read -rsp "       Repitela: " PW2; echo
    [ "$PW1" = "$PW2" ] && break
    echo "       No coinciden."
  done
  set_env PANEL_SEED_PASSWORD "$PW1"
  unset PW1 PW2
fi

if grep -q '^PANEL_SEED_PASSWORD=.\+' .env; then
  if ./.venv/bin/python -m server.users sync 2>&1 | sed 's/^/       /'; then
    ok "usuarios creados con esa contrasena"
  else
    bad "no pude crear los usuarios"; FAILED=1
  fi
else
  bad "PANEL_SEED_PASSWORD esta vacio en .env -> NINGUNA contrasena funcionara"
  echo "       Ponla y vuelve a lanzar:  nano .env   (PANEL_SEED_PASSWORD=tu-contrasena)"
  FAILED=1
fi

if grep -q '^PANEL_PIN=.\+' .env && ! grep -q '^PANEL_PIN_HASH=.\+' .env; then
  warn "el PIN esta en texto plano; funciona, pero mejor pasarlo a hash:"
  echo "       ./.venv/bin/python -m server.hashpw --pin"
fi

# --------------------------------------------------------------------------- #
step "5/7  Servicio"
# --------------------------------------------------------------------------- #
install -m 644 deploy/sniper-deck.service /etc/systemd/system/sniper-deck.service
sed -i "s#/root/sniper-deck#${APP_DIR}#g" /etc/systemd/system/sniper-deck.service
systemctl daemon-reload
systemctl enable --now sniper-deck >/dev/null 2>&1
systemctl restart sniper-deck
sleep 2
if [ "$(systemctl is-active sniper-deck)" = "active" ]; then
  ok "panel corriendo"
else
  bad "el panel no arranca. Mira:  journalctl -u sniper-deck -n 40 --no-pager"; FAILED=1
fi

if curl -fsS --max-time 8 http://127.0.0.1:8080/api/ping >/dev/null 2>&1; then
  ok "responde en local"
else
  bad "no responde en 127.0.0.1:8080"; FAILED=1
fi

# --------------------------------------------------------------------------- #
step "6/7  HTTPS (para que la contrasena no viaje en claro)"
# --------------------------------------------------------------------------- #
MY_IP="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo '')"
API_HOST="${API_HOST:-${MY_IP//./-}.sslip.io}"
echo "       IP publica : ${MY_IP:-?}"
echo "       Hostname   : $API_HOST"

HOST_IP="$(getent hosts "$API_HOST" 2>/dev/null | awk '{print $1}' | head -1)"
if [ -z "$HOST_IP" ]; then
  bad "$API_HOST no resuelve. Crea el registro A -> $MY_IP y relanza."
  FAILED=1
elif [ "$HOST_IP" != "$MY_IP" ]; then
  bad "$API_HOST resuelve a $HOST_IP, no a $MY_IP. Corrige el DNS y relanza."
  FAILED=1
else
  ok "$API_HOST apunta aqui"

  if ! command -v caddy >/dev/null 2>&1; then
    echo "       instalando Caddy..."
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null 2>&1
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      > /etc/apt/sources.list.d/caddy-stable.list 2>/dev/null
    apt-get update -qq >/dev/null 2>&1
    apt-get install -y -qq caddy >/dev/null 2>&1
  fi

  if command -v caddy >/dev/null 2>&1; then
    sed "s#^API_HOST#${API_HOST}#" deploy/Caddyfile > /etc/caddy/Caddyfile
    if caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
      systemctl enable --now caddy >/dev/null 2>&1
      systemctl reload caddy 2>/dev/null || systemctl restart caddy
      echo "       esperando el certificado (hasta 60s)..."
      for _ in $(seq 1 20); do
        if curl -fsS --max-time 5 "https://${API_HOST}/api/ping" >/dev/null 2>&1; then
          HTTPS_OK=1; break
        fi
        sleep 3
      done
      if [ "$HTTPS_OK" = 1 ]; then
        ok "HTTPS funcionando en https://${API_HOST}"
      else
        bad "Caddy no consiguio el certificado. Mira:  journalctl -u caddy -n 40 --no-pager"
        [ "${API_HOST}" != "${API_HOST%sslip.io}" ] && \
          echo "       sslip.io a veces choca con los limites de Let's Encrypt: usa un subdominio propio."
        FAILED=1
      fi
    else
      bad "el Caddyfile no valida"; FAILED=1
    fi
  else
    bad "no pude instalar Caddy"; FAILED=1
  fi
fi

# --------------------------------------------------------------------------- #
step "7/7  Cortafuegos"
# --------------------------------------------------------------------------- #
if [ "$HTTPS_OK" = 1 ] && command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp  >/dev/null 2>&1
  ufw allow 80/tcp  >/dev/null 2>&1
  ufw allow 443/tcp >/dev/null 2>&1
  ufw delete allow 8080/tcp >/dev/null 2>&1
  ufw deny 8080/tcp >/dev/null 2>&1
  ufw --force enable >/dev/null 2>&1
  if curl -fsS --max-time 5 "http://${MY_IP}:8080/api/ping" >/dev/null 2>&1; then
    bad "el 8080 sigue accesible desde fuera: cierralo en el firewall de DigitalOcean"
    FAILED=1
  else
    ok "8080 cerrado a internet; solo 22/80/443"
  fi
elif [ "$HTTPS_OK" != 1 ]; then
  warn "no cierro el 8080 todavia: sin HTTPS te quedarias sin acceso."
  ufw allow 8080/tcp >/dev/null 2>&1 || true
fi

# --------------------------------------------------------------------------- #
printf '\n\033[1m========================= RESUMEN =========================\033[0m\n'
# --------------------------------------------------------------------------- #
if [ "$HTTPS_OK" = 1 ] && [ "$FAILED" = 0 ]; then
  cat <<EOF

  Todo listo y cifrado.

  1) Pon esto en vercel.json (en tu PC) y haz push:

       { "source": "/api/(.*)", "destination": "https://${API_HOST}/api/\$1" }

  2) Entra en la web de Vercel con tu usuario y contrasena.

  3) Cuando hayas comprobado que entras, para operar con dinero:

       sed -i 's/^PANEL_HOT_WALLET=.*/PANEL_HOT_WALLET=true/' ${APP_DIR}/.env
       systemctl restart sniper-deck

     Y haz una primera compra minuscula (0.02 SOL) antes de mover nada serio.

EOF
else
  cat <<EOF

  Quedan cosas por resolver (mira los FALLO de arriba).

  El panel esta funcionando en local del droplet, pero sin HTTPS verificado NO
  he cerrado el 8080 (te quedarias fuera). Eso significa que, de momento, la
  contrasena viaja en claro: PANEL_HOT_WALLET sigue en false a proposito.

  Lo mas comun:
    - PANEL_SEED_PASSWORD vacio en .env  -> ninguna contrasena funciona
    - el hostname no resuelve a este droplet -> crea el registro A y relanza
    - Let's Encrypt rechazo sslip.io    -> usa un subdominio tuyo:
        API_HOST=api-banco.tudominio.com bash deploy/listo.sh

EOF
fi

echo "  Estado:  panel=$(systemctl is-active sniper-deck 2>/dev/null)  caddy=$(systemctl is-active caddy 2>/dev/null)"
echo "  Usuarios:"
./.venv/bin/python -m server.users list 2>/dev/null | sed 's/^/  /'
echo
exit "$FAILED"
