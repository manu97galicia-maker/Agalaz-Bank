# Poner la web app online (verla en el móvil)

El **cerebro corre en el droplet** (lee el bot, controla el server, firma la
wallet). Vercel no puede ejecutar eso: como mucho hace de **proxy** hacia el
droplet. Por eso el orden es: **1) droplet vivo y accesible → 2) Vercel apunta a
él**.

---

## Paso 1 — Panel corriendo en el droplet

```bash
ssh root@159.89.19.12

# clonar (o actualizar) y instalar
[ -d /root/sniper-deck ] && (cd /root/sniper-deck && git pull) \
  || git clone https://github.com/manu97galicia-maker/WEB-APP-DATOS-FRANC-.git /root/sniper-deck
cd /root/sniper-deck
bash deploy/install.sh
./.venv/bin/pip install solders          # wallet caliente

# credenciales
./.venv/bin/python -m server.hashpw          # -> PANEL_PASSWORD_HASH  (tu @Otito123)
./.venv/bin/python -m server.hashpw --pin     # -> PANEL_PIN_HASH        (tu 1357)
nano .env
#   pega los dos hashes
#   PANEL_USER=manu
#   PANEL_HOST=0.0.0.0        <- para que Vercel pueda llegar (ver Paso 2)
#   PANEL_COOKIE_SECURE=true  <- el navegador entra por HTTPS (Vercel)
#   PANEL_HOT_WALLET=false    <- déjalo OFF hasta tener HTTPS de punta a punta

systemctl restart sniper-deck
systemctl status sniper-deck --no-pager

# abrir el puerto para que Vercel lo alcance
ufw allow 8080/tcp || true       # y/o el firewall de DigitalOcean en el panel web
```

Comprueba desde tu PC: `http://159.89.19.12:8080/api/ping` debe devolver
`{"ok": true}`.

---

## Paso 2 — Vercel proxya al droplet

Ya está el `vercel.json` en el repo:

```json
{ "rewrites": [{ "source": "/(.*)", "destination": "http://159.89.19.12:8080/$1" }] }
```

El proyecto `web-app-datos-franc` está conectado a este repo, así que **cada push
lo redespliega**. En `https://web-app-datos-franc.vercel.app/` el navegador habla
HTTPS con Vercel y Vercel reenvía al droplet. Entra con el **PIN 1357**.

> El tramo Vercel→droplet va por HTTP plano. Para el MVP (viendo datos, wallet
> caliente OFF) vale. **Antes de encender `PANEL_HOT_WALLET=true`**, monta el
> Paso 3 para tener HTTPS de punta a punta.

---

## Paso 3 (recomendado antes de operar con dinero) — dominio + HTTPS real

Con un dominio propio evitas el HTTP plano y el panel deja de estar desnudo en
la IP:

```bash
apt install -y caddy
cp deploy/Caddyfile /etc/caddy/Caddyfile
nano /etc/caddy/Caddyfile     # pon panel.TUDOMINIO.com y su DNS -> 159.89.19.12
systemctl reload caddy
```

Luego, o bien:
- usas directamente `https://panel.tudominio.com` en el móvil (sin Vercel), o
- cambias el `destination` del `vercel.json` a `https://panel.tudominio.com/$1`
  y vuelves a `PANEL_HOST=127.0.0.1` + cierras el puerto 8080.

Con eso ya es seguro poner `PANEL_HOT_WALLET=true` y operar.
