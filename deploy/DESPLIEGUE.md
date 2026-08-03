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
  || git clone https://github.com/manu97galicia-maker/Agalaz-Bank.git /root/sniper-deck
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
HTTPS con Vercel y Vercel reenvía al droplet. Entra con tu usuario y
contraseña, o con el PIN que hayas puesto en `PANEL_PIN_HASH`.

> El tramo Vercel→droplet va por HTTP plano. Para el MVP (viendo datos, wallet
> caliente OFF) vale. **Antes de encender `PANEL_HOT_WALLET=true`**, monta el
> Paso 3 para tener HTTPS de punta a punta.

---

## Paso 3 (OBLIGATORIO antes de operar con dinero) — cerrar el droplet

Sin este paso el montaje tiene dos agujeros de verdad:

1. El tramo Vercel→droplet va en **texto plano**: tu contraseña viaja sin cifrar.
2. El **puerto 8080 está abierto a todo internet**: cualquiera puede escanear la
   IP y llamar a la API directamente, saltándose Vercel.

Una contraseña más larga no arregla ninguno de los dos. Hay que cifrar el tramo
y cerrar el puerto.

### Hace falta un nombre, no una IP

Let's Encrypt no firma certificados para una IP pelada, así que el droplet
necesita un hostname. **Tú no lo vas a usar**: sigues abriendo la URL de Vercel.
Es solo el extremo cifrado.

- **Lo fiable:** un subdominio tuyo, p.ej. `api-banco.agalaz.com`, con un
  registro **A → 159.89.19.12**.
- **Sin tocar DNS:** `159-89-19-12.sslip.io` resuelve solo a esa IP. Depende de
  un servicio de terceros y esos dominios compartidos a veces chocan con los
  límites de emisión de Let's Encrypt; si falla, usa la opción de arriba.

### Ejecutar

```bash
cd /root/sniper-deck && git pull
API_HOST=api-banco.tudominio.com bash deploy/harden.sh
```

El script comprueba que el DNS apunta aquí, instala Caddy, saca el certificado,
pone el panel a escuchar solo en `127.0.0.1`, cierra el 8080 en el cortafuegos y
verifica las dos cosas al final.

### Y apuntar Vercel al extremo cifrado

En `vercel.json`:

```json
{ "source": "/api/(.*)", "destination": "https://api-banco.tudominio.com/api/$1" }
```

`git push` y Vercel redespliega solo. Los `rewrites` son un **proxy de servidor**,
así que el navegador sigue viendo un único origen y la cookie de sesión no se
rompe.

Con esto sí es seguro poner `PANEL_HOT_WALLET=true`.

---

## Qué queda protegiendo el panel (y qué no)

Después del paso 3:

| Capa | Estado |
|---|---|
| Cifrado de punta a punta | ✅ navegador→Vercel→droplet, todo HTTPS |
| Puerto 8080 desde internet | ✅ cerrado |
| Contraseña | ✅ PBKDF2-SHA256, 240k rondas |
| PIN corto | ✅ hash aparte |
| Face ID / WebAuthn | ✅ |
| Bloqueo por intentos | ✅ creciente, hasta 6 h |
| Firma de transacciones | ✅ apagada salvo `PANEL_HOT_WALLET=true` |

Lo que **no** desaparece: la URL de la API sigue siendo pública (es el precio de
tener una web pública que abres desde el móvil). Lo que la protege es la
cerradura, no el escondite. Por eso importa que la contraseña sea buena y que el
bloqueo por intentos esté activo.

Si algún día quieres que *ni siquiera se pueda llamar a la puerta*, el camino es
Tailscale: el panel deja de estar en internet y solo lo ven tus dispositivos.
Pierdes la web pública y el poder compartirla con Ricardo sin meterlo en la VPN.
