# AGALAZ BANK — panel de trading

> **Crypto · Future · Freedom**

Web app privada, pensada para el móvil, que maneja el bot de sniping de Solana
([`Definitivo_bot`](https://github.com/manu97galicia-maker/Definitivo_bot)) y
opera la wallet: ver posiciones y P&L, **comprar y vender** tokens, mirar el
servidor, editar las listas de wallets que copia el bot, repartir ganancias y
darle órdenes **en lenguaje natural** por chat.

Corre **en el mismo droplet que el bot** (159.89.19.12). No es un detalle: es lo
que le permite leer el estado del bot en vivo, hablar con systemd y firmar con
la wallet sin abrir canales extra.

```
   móvil ──HTTPS──> Vercel/Caddy ──> AGALAZ BANK (droplet) ──ficheros──> Definitivo_bot
                                          │
                                          ├──> Solana RPC + Jupiter  (leer y FIRMAR)
                                          ├──> DexScreener / RugCheck (mercado)
                                          └──> Claude API            (el chat)
```

---

## Qué hace, pestaña a pestaña

| Pestaña | Qué hace |
|---|---|
| **Chat** | «¿cómo voy hoy?», «vende la mitad de la que va ganando», «mira este token», «¿está todo corriendo?». Puede consultar mercado y **proponer** compras/ventas/envíos/cambios de lista (tú confirmas). |
| **Comprar/Vender** | Pega un contrato → precio, market cap, liquidez, volumen y veredicto de rug + enlaces a DexScreener/GMGN/Solscan. Botones de **comprar** (0.05–0.5 SOL) y **vender** (25/50/100 %). Firma real vía Jupiter. |
| **Panel** | Gráfico del **P&L total desde el inicio** + barras **por moneda**, y vender lo que tengas abierto (25/50/100 %). |
| **Posiciones** | Lo abierto ahora con P&L en vivo, resultados de hoy y del histórico, y la foto real del wallet on-chain (valorada en USD). |
| **Listas** | Editar las listas `top_devs_*.json` / `blacklist_devs.json` que el bot copia: añadir o quitar wallets de developers/traders. |
| **Ganancias** | Reparto del beneficio por **% a cada wallet**, en modo **manual** (repartes tú) o **automático** (cada 5 min manda la parte nueva). |
| **Bots** | Los configs YAML: activar/desactivar, ver estrategia, TP/SL, tamaño de compra. |
| **Servidor** | Carga, disco, memoria, procesos del bot vivos, estado de los servicios systemd y logs. |

---

## Acceso

- **Multiusuario con cargos.** Los administradores viven en `state/users.json`
  (fuera del repo). Por defecto: **Manu — Chief Trololo Officer** y **Ricardo —
  Chief Technological Trololo Officer**. El cargo se ve al lado del nombre dentro
  de la app.
- **Contraseña** (la que sembraste en `PANEL_SEED_PASSWORD`) **o Face ID /
  huella** (WebAuthn: Face ID,
  Windows Hello, huella Android) **o PIN** corto desde el móvil.
- **Recuérdame:** sesión persistente de 30 días; sin marcarlo, caduca al cerrar
  el navegador.
- Defensas: contraseñas PBKDF2-SHA256 (240k rondas), cookie de sesión firmada
  con HMAC que lleva el usuario dentro, y bloqueo progresivo por IP tras fallos.

**Face ID es fail-closed:** cualquier fallo de verificación deja fuera y se cae
a la contraseña; nunca la salta. Necesita HTTPS y un dominio (o localhost); **no
funciona sobre una IP pelada**.

### Crear / cambiar usuarios

```bash
python -m server.users list
python -m server.users add Ricardo "Chief Technological Trololo Officer"
```

O se siembran Manu y Ricardo en el primer arranque poniendo
`PANEL_SEED_PASSWORD` en el `.env` (se hashea; ninguna contraseña acaba en el
repo).

---

## Cómo habla con el bot

El bot ya se comunica por ficheros en su propio repo, y el panel usa ese mismo
canal en vez de inventarse otro:

| Fichero | Uso |
|---|---|
| `open_position_<bot>.json` | posición viva, la escribe el bot cada ~2 s |
| `sell_cmd_<bot>.json` | orden de venta: el bot la lee, ejecuta y borra |
| `trades/trades.log` | log de fills (JSON-lines) → P&L realizado y gráficos |
| `bots/<bot>.yaml` | estrategia y filtros |
| `top_devs_*.json`, `blacklist_devs.json` | a quién copia el bot |
| `logs/<bot>_<ts>.log` | logs de ejecución |

---

## Seguridad — qué puede y qué no

**El agente (chat) nunca actúa solo.** Las herramientas de lectura (P&L,
posiciones, wallet, mercado, logs, salud del server) se ejecutan solas. Todo lo
que mueve dinero o cambia algo —comprar, vender, enviar SOL, editar una lista,
tocar un config, reiniciar/parar un servicio, shell— **solo deja una propuesta**
que confirmas a mano (caducan a los 10 min).

### 🔴 Wallet caliente (opcional, apagada por defecto)

Con `PANEL_HOT_WALLET=false` (por defecto) el panel es **solo lectura + órdenes
al bot**: no hay ninguna ruta que firme. Con `PANEL_HOT_WALLET=true` **firma
transacciones reales** (compra/venta vía Jupiter, envío de SOL para repartir
ganancias). Eso lo convierte en una wallet caliente: quien comprometa el panel
puede mover los fondos. Manu lo eligió a conciencia. Defensas al encenderla:

- **Tope duro** `PANEL_MAX_BUY_SOL` por compra.
- **Un solo secreto**: reutiliza la clave del bot (`SOLANA_PRIVATE_KEY`), no hay
  segunda copia.
- Cambios de config limitados a una lista blanca de campos numéricos; systemd a
  una lista blanca de unidades; nombres de bot validados (nada de `../`).
- La shell libre está apagada (`AGENT_ALLOW_SHELL=false`).

> **Antes de operar con dinero de verdad, monta HTTPS de punta a punta**
> (dominio + Caddy, ver Despliegue). No dejes la wallet caliente encendida detrás
> de HTTP plano.

---

## Módulos

```
server/
  app.py            HTTP: auth, rutas, middleware, bucle de reparto automático
  auth.py           multiusuario, tokens de sesión firmados, bloqueo por IP
  users.py          store de usuarios + cargos (state/users.json) + CLI
  webauthn_face.py  Face ID / huella (WebAuthn, fail-closed)
  botlink.py        puente con los ficheros del bot (posiciones, P&L, configs, listas)
  chain.py          lectura on-chain del wallet (saldo + tokens, valorado con Jupiter)
  wallet.py         FIRMA: comprar/vender (Jupiter) + enviar SOL      [wallet caliente]
  market.py         info de token (DexScreener + RugCheck)
  lists.py          CRUD de las listas de wallets del bot
  profits.py        reparto de ganancias por % (manual / automático)
  agent.py          chat en lenguaje natural (Claude) con herramientas propone→confirma
  ops.py            operaciones del droplet (systemd, ps, disco) con lista blanca
  config.py         toda la configuración desde el entorno
  static/index.html la web app entera (un fichero, sin recursos externos)
```

---

## Despliegue

Guía paso a paso en [`deploy/DESPLIEGUE.md`](deploy/DESPLIEGUE.md). En corto:

### En el droplet (el cerebro)

```bash
ssh root@159.89.19.12
[ -d /root/sniper-deck ] && (cd /root/sniper-deck && git pull) \
  || git clone https://github.com/manu97galicia-maker/Agalaz-Bank.git /root/sniper-deck
cd /root/sniper-deck && bash deploy/install.sh   # instala aiohttp, PyYAML, anthropic, solders, webauthn
./.venv/bin/python -m server.hashpw --pin        # PIN opcional
nano .env                                         # PANEL_SEED_PASSWORD, PANEL_HOST, etc.
systemctl restart sniper-deck
```

### Que se vea online

- **Vercel** (ya hay `vercel.json`): sirve el frontend y **proxya `/api/*` al
  droplet**. Conecta el repo en Vercel y cada push redespliega. La app se ve en
  `web-app-datos-franc.vercel.app`; para que funcione el login, el droplet tiene
  que estar vivo y accesible (`PANEL_HOST=0.0.0.0` + puerto 8080 abierto).
- **Dominio propio (recomendado antes de operar):** `deploy/Caddyfile` pone
  HTTPS automático delante del panel. Usa `https://panel.tudominio.com` en el
  móvil, o apunta ahí el `destination` del `vercel.json`.

---

## Desarrollo local

```bash
python -m venv .venv && .venv/Scripts/activate     # Windows
pip install aiohttp PyYAML anthropic solders webauthn
copy .env.example .env                              # BOT_REPO a tu ruta local,
                                                    # PANEL_SEED_PASSWORD, PANEL_COOKIE_SECURE=false
python -m server                                    # http://localhost:8080
```

El chat necesita `ANTHROPIC_API_KEY`; sin ella, el resto funciona. `solders` y
`webauthn` son import perezoso: sin ellos, todo funciona menos la firma y el
Face ID. Las operaciones de systemd/`ps` no existen en Windows y devuelven un
error claro; el resto del panel funciona igual.

> **El repo es privado.** No lleva secretos (`.gitignore` excluye `.env` y
> `state/`), pero un panel de control de tu wallet no tiene por qué ser un mapa
> público de tu infraestructura.
