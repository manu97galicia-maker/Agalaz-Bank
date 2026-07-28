# Sniper Deck

Panel web privado para manejar el bot de sniping
([`Definitivo_bot`](https://github.com/manu97galicia-maker/Definitivo_bot)) desde
el móvil: ver posiciones y P&L, vender, tocar configs, mirar el servidor y
darle órdenes **en lenguaje natural**.

Corre **en el mismo droplet que el bot** (159.89.19.12). Eso no es un detalle de
implementación: es lo que permite leer los ficheros de estado del bot en directo
y hablar con systemd sin abrir un canal remoto extra.

```
   móvil ──HTTPS──> Caddy ──127.0.0.1:8080──> Sniper Deck ──ficheros──> Definitivo_bot
                                                    │
                                                    ├──> Solana RPC  (solo lectura)
                                                    └──> Claude API  (el chat)
```

---

## Qué hace

| | |
|---|---|
| **Posiciones** | Lo que hay abierto ahora, con P&L en vivo. Botones de vender 50 % / 100 % |
| **Resultados** | P&L realizado, ROI y win rate de hoy y del histórico |
| **Wallet** | Saldo real on-chain + tokens que aún tiene, valorados en USD (Jupiter) |
| **Bots** | Los 19 configs: activar/desactivar, ver estrategia, leer el YAML |
| **Servidor** | Carga, disco, procesos del bot vivos, estado de los servicios, logs |
| **Chat** | «¿cómo voy hoy?», «vende la mitad de la que va ganando», «¿está corriendo todo?» |

---

## Cómo habla con el bot

El bot ya se comunica por ficheros en su propio repo, y el panel usa ese mismo
canal en vez de inventarse otro:

| Fichero | Uso |
|---|---|
| `open_position_<bot>.json` | posición viva, la escribe el bot cada ~2 s |
| `sell_cmd_<bot>.json` | orden de venta: el bot la lee, ejecuta y borra |
| `trades/trades.log` | log de fills (JSON-lines) → P&L realizado |
| `bots/<bot>.yaml` | estrategia y filtros |
| `logs/<bot>_<ts>.log` | logs de ejecución |

Es exactamente el mismo camino que usa `tg_panel.py` en producción, así que una
venta desde el panel toma la ruta que ya sabes que funciona.

**El panel nunca importa el código del bot ni firma transacciones.** De la clave
privada solo deriva la dirección pública, para poder leer el wallet.

---

## Seguridad: qué puede y qué no puede hacer el agente

Esto es lo importante. El chat está conectado a un servidor que guarda la clave
privada de un wallet con dinero real, así que el reparto es explícito:

**Se ejecuta solo** (no hay nada que perder): consultar posiciones, P&L, trades,
wallet, configs, logs y salud del servidor.

**No se ejecuta nunca solo** — el agente solo *deja una propuesta*, y no pasa
nada hasta que tú pulsas **Confirmar** viendo el texto exacto:

- vender parte de una posición
- activar / desactivar un bot
- cambiar un parámetro de trading
- reiniciar o parar un servicio
- ejecutar un comando de shell

Las propuestas caducan a los 10 minutos, para que una venta preparada no se
confirme una hora después contra una posición que ya no existe.

Además:

- **La shell libre está apagada** (`AGENT_ALLOW_SHELL=false`). Enciéndela solo si
  la necesitas; aun así siempre pasa por confirmación.
- Los cambios de config están limitados a una lista blanca de campos numéricos
  (`buy_amount`, `take_profit_percentage`, `stop_loss_percentage`, slippages,
  `max_token_age`…). Nada estructural: listener, plataforma o wallet siguen
  siendo edición a mano.
- systemd solo acepta unidades de una lista blanca (autodetectadas por prefijo o
  fijadas en `PANEL_ALLOWED_UNITS`).
- Los nombres de bot se validan contra `[A-Za-z0-9._-]`, así que ni el modelo ni
  una petición trucada pueden salirse del repo con `../`.
- **No hay retirada de fondos.** El panel no tiene ese código.

Y por parte del acceso: contraseña con PBKDF2-SHA256 (240k rondas), cookie de
sesión firmada con HMAC, bloqueo progresivo por IP tras fallos de login, y
`Secure`+`HttpOnly`+`SameSite=Lax` en la cookie.

> **El repo de GitHub está en público.** Ponlo en privado
> (`Settings → General → Danger Zone → Change visibility`). El código no lleva
> secretos y `.gitignore` excluye `.env`, pero un panel de control de tu bot no
> tiene por qué ser un mapa público de tu infraestructura.

---

## Instalación en el droplet

```bash
ssh root@159.89.19.12

git clone https://github.com/manu97galicia-maker/WEB-APP-DATOS-FRANC-.git /root/sniper-deck
cd /root/sniper-deck
bash deploy/install.sh
```

El instalador crea el venv, genera `.env` con un `PANEL_SESSION_SECRET`
aleatorio e instala el servicio systemd. Falta poner la contraseña:

```bash
./.venv/bin/python -m server.hashpw     # imprime PANEL_PASSWORD_HASH=...
nano .env                               # pega el hash y pon PANEL_USER
systemctl start sniper-deck
systemctl status sniper-deck --no-pager
```

### Publicarlo

**Con dominio (recomendado).** Apunta un subdominio al droplet, copia
`deploy/Caddyfile`, cambia el dominio y recarga Caddy. Caddy saca el
certificado solo:

```bash
apt install -y caddy
cp deploy/Caddyfile /etc/caddy/Caddyfile && nano /etc/caddy/Caddyfile
systemctl reload caddy
```

**Sin dominio.** Túnel SSH, y el panel no toca internet:

```bash
ssh -L 8080:127.0.0.1:8080 root@159.89.19.12
# abre http://127.0.0.1:8080  (pon PANEL_COOKIE_SECURE=false en .env)
```

El panel escucha en `127.0.0.1` por defecto: **no** lo pongas en `0.0.0.0` para
ahorrarte el proxy. Sin TLS, la contraseña viaja en claro.

---

## El chat

Necesita `ANTHROPIC_API_KEY` en `.env`. Sin ella todo lo demás sigue
funcionando; solo desaparece el chat.

Ejemplos que entiende:

- «¿cómo voy hoy?» → mira el log de fills y te da P&L, ROI y aciertos
- «¿qué tengo abierto?» → posiciones vivas con su P&L
- «vende la mitad de la que va ganando» → **deja preparada** la venta del 50 %
- «¿cuánto vale mi wallet ahora mismo?» → consulta la cadena y valora en USD
- «¿está todo corriendo?» → carga, disco, procesos y servicios
- «sube el stop loss del bot elite al 25 %» → **propone** el cambio y te avisa
  de que hay que reiniciar el bot para que aplique

`AGENT_EFFORT` regula cuánto razona (`low`…`max`). `medium` va rápido; sube a
`high` si le haces preguntas de estrategia.

---

## Notas que ahorran sustos

- **Cambiar un YAML no afecta a un bot ya arrancado.** El proceso lleva su
  config cargada en memoria. Si quieres que pare *ahora*, para el proceso.
- **Una venta se encola, no se ejecuta.** El panel escribe `sell_cmd`; el bot lo
  recoge en su siguiente vuelta. Si el bot está caído, la orden se queda ahí.
- **El P&L de `trades.log` es una estimación** a partir de los fills logueados,
  no una auditoría on-chain. La pestaña Wallet es la verdad.
- El historial del chat vive en memoria: si reinicias el servicio, se pierde.
  Es a propósito — lleva una foto del wallet dentro.

---

## Desarrollo local

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install aiohttp PyYAML anthropic
copy .env.example .env                            # pon BOT_REPO a tu ruta local
python -m server
```

Las operaciones de systemd/`ps` no existen en Windows y devuelven un error
claro; el resto del panel funciona igual.
