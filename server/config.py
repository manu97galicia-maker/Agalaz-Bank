"""Configuration for the sniper control panel.

Everything comes from the environment. The panel deliberately reads the bot's
own ``.env`` for the RPC endpoint and the wallet, so there is exactly one place
where those live (the bot repo) and the panel never needs its own copy of the
private key.

The private key itself is only ever used to derive the *public* key. The panel
has no code path that signs a transaction.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# .env loading (no dependency on python-dotenv)                                #
# --------------------------------------------------------------------------- #


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=value file, ignoring comments and blank lines."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _load_env() -> None:
    """Load this app's .env into os.environ without overriding real env vars."""
    for key, value in _parse_env_file(APP_ROOT / ".env").items():
        os.environ.setdefault(key, value)


APP_ROOT = Path(__file__).resolve().parents[1]
_load_env()


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Paths                                                                        #
# --------------------------------------------------------------------------- #

#: Checkout of https://github.com/manu97galicia-maker/Definitivo_bot
BOT_REPO = Path(os.getenv("BOT_REPO", "/root/Definitivo_bot")).expanduser()

BOT_ENV: dict[str, str] = _parse_env_file(BOT_REPO / ".env")


def bot_env(name: str, default: str = "") -> str:
    """Read a variable from the bot's .env, falling back to our own env."""
    return os.getenv(name) or BOT_ENV.get(name, default)


# --------------------------------------------------------------------------- #
# Server                                                                       #
# --------------------------------------------------------------------------- #

HOST = os.getenv("PANEL_HOST", "127.0.0.1")
PORT = int(os.getenv("PANEL_PORT", "8080"))

#: Only set this to False behind a TLS terminator you control. When True the
#: session cookie carries the Secure flag and will not travel over plain HTTP.
COOKIE_SECURE = _flag("PANEL_COOKIE_SECURE", True)

PANEL_USER = os.getenv("PANEL_USER", "")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")
PANEL_PASSWORD_HASH = os.getenv("PANEL_PASSWORD_HASH", "")
SESSION_SECRET = os.getenv("PANEL_SESSION_SECRET", "")
SESSION_TTL_SECONDS = int(os.getenv("PANEL_SESSION_TTL", str(60 * 60 * 24 * 14)))
#: TTL de la sesión cuando marcas "recuérdame" (por defecto 30 días). Sin
#: marcarlo, la cookie caduca al cerrar el navegador y el token dura 1 día.
REMEMBER_TTL_SECONDS = int(os.getenv("PANEL_REMEMBER_TTL", str(60 * 60 * 24 * 30)))
SHORT_TTL_SECONDS = int(os.getenv("PANEL_SHORT_TTL", str(60 * 60 * 24)))

# --- Face ID / huella (WebAuthn) ---
#: Nombre e ID del "relying party". El RP_ID debe ser el dominio (sin puerto):
#: localhost en local, o web-app-datos-franc.vercel.app en producción. Vacío =>
#: se deriva del Host de cada petición. Nota: WebAuthn NO funciona sobre una IP
#: pelada, hace falta dominio o localhost + HTTPS.
RP_NAME = os.getenv("PANEL_RP_NAME", "AGALAZ BANK")
RP_ID = os.getenv("PANEL_RP_ID", "")
PASSKEYS_FILE = Path(os.getenv("PANEL_PASSKEYS_FILE", str(APP_ROOT / "state" / "passkeys.json")))

#: PIN corto de 4+ dígitos como atajo de entrada desde el móvil (además de la
#: contraseña). Va HASHEADO igual que la contraseña; el .env solo guarda el hash.
#: Genera el hash con `python -m server.hashpw --pin`.
PANEL_PIN_HASH = os.getenv("PANEL_PIN_HASH", "")
#: Solo para primer arranque cómodo: PIN en claro. Se hashea al importar y el
#: panel te avisa de que lo pases a PANEL_PIN_HASH.
PANEL_PIN = os.getenv("PANEL_PIN", "")

# --------------------------------------------------------------------------- #
# Usuarios administradores (multiusuario)                                      #
# --------------------------------------------------------------------------- #
#: Fichero con los usuarios y sus cargos (se crea solo, gitignored). Cada
#: entrada: {"user","role","password_hash"}. Editable con `python -m server.users`.
USERS_FILE = Path(os.getenv("PANEL_USERS_FILE", str(APP_ROOT / "state" / "users.json")))
#: Cargos por defecto (no son secreto). Se usan al sembrar usuarios nuevos.
DEFAULT_ROLES = {
    "manu": "Chief Trololo Officer",
    "ricardo": "Chief Technological Trololo Officer",
}
#: Si está puesta, en el primer arranque se siembran Manu y Ricardo con esta
#: contraseña (hasheada). Así no hay ninguna contraseña en claro en el repo.
SEED_PASSWORD = os.getenv("PANEL_SEED_PASSWORD", "")

# --------------------------------------------------------------------------- #
# Solana (read-only)                                                           #
# --------------------------------------------------------------------------- #

FALLBACK_RPC = "https://api.mainnet-beta.solana.com"
RPC_ENDPOINT = bot_env("SOLANA_NODE_RPC_ENDPOINT") or FALLBACK_RPC
#: Set explicitly to avoid touching the private key at all.
WALLET_PUBKEY = os.getenv("PANEL_WALLET_PUBKEY", "")
JUPITER_PRICE_API = os.getenv("JUPITER_PRICE_API", "https://lite-api.jup.ag/price/v3")
JUPITER_API = os.getenv("JUPITER_API", "https://lite-api.jup.ag")

# --------------------------------------------------------------------------- #
# Wallet caliente (FIRMA transacciones reales)                                 #
# --------------------------------------------------------------------------- #
#
# ¡Ojo! Con esto en true la web PUEDE MOVER TU DINERO: compra, vende y envía SOL
# firmando con la clave privada del bot. Si comprometen el panel, pueden vaciar
# el wallet. Está APAGADO por defecto a propósito: enciéndelo tú, a conciencia.
HOT_WALLET = _flag("PANEL_HOT_WALLET", False)
#: Clave privada que firma. Por defecto reutiliza la MISMA del bot (un solo
#: sitio con el secreto). Puedes poner otra en el .env del panel si quieres una
#: wallet aparte para operar a mano.
def wallet_secret() -> str:
    return os.getenv("PANEL_WALLET_SECRET") or bot_env("SOLANA_PRIVATE_KEY")

#: Topes de seguridad para una compra manual desde la web. Una web con un botón
#: que gasta SOL real necesita un límite duro por si un dedo resbala.
MAX_BUY_SOL = float(os.getenv("PANEL_MAX_BUY_SOL", "1.0"))
DEFAULT_BUY_SLIPPAGE_BPS = int(os.getenv("PANEL_BUY_SLIPPAGE_BPS", "1500"))   # 15%
DEFAULT_SELL_SLIPPAGE_BPS = int(os.getenv("PANEL_SELL_SLIPPAGE_BPS", "2500"))  # 25%
#: Fee de prioridad (lamports) que Jupiter añade para que la tx entre rápido.
PRIORITY_FEE_LAMPORTS = int(os.getenv("PANEL_PRIORITY_FEE_LAMPORTS", "2000000"))

#: Dónde guarda el panel su propio estado editable (reparto de ganancias, etc.).
STATE_DIR = Path(os.getenv("PANEL_STATE_DIR", str(APP_ROOT / "state")))

# --------------------------------------------------------------------------- #
# Agent                                                                        #
# --------------------------------------------------------------------------- #

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-opus-5")
#: low | medium | high | xhigh | max. Medium keeps the panel responsive; raise
#: it if you want the agent to reason harder about strategy questions.
AGENT_EFFORT = os.getenv("AGENT_EFFORT", "medium")
AGENT_MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "8000"))
#: Hard ceiling on tool-call rounds per message, so a confused model cannot
#: loop forever against the droplet.
AGENT_MAX_ROUNDS = int(os.getenv("AGENT_MAX_ROUNDS", "8"))

#: Off by default. When enabled the agent may *propose* a shell command, but it
#: is never executed until you press Confirm and read the exact command first.
AGENT_ALLOW_SHELL = _flag("AGENT_ALLOW_SHELL", False)

#: systemd units the panel is allowed to inspect/restart. Empty => auto-detect
#: units whose name starts with "sniper" or "pumpbot".
ALLOWED_UNITS = [
    u.strip() for u in os.getenv("PANEL_ALLOWED_UNITS", "").split(",") if u.strip()
]


def public_config() -> dict[str, Any]:
    """Non-secret configuration echoed to the browser for UI decisions."""
    return {
        "bot_repo": str(BOT_REPO),
        "bot_repo_found": BOT_REPO.is_dir(),
        "agent_enabled": bool(ANTHROPIC_API_KEY),
        "agent_model": AGENT_MODEL,
        "shell_enabled": AGENT_ALLOW_SHELL,
        "rpc_configured": RPC_ENDPOINT != FALLBACK_RPC,
        "hot_wallet": HOT_WALLET,
        "max_buy_sol": MAX_BUY_SOL,
    }
