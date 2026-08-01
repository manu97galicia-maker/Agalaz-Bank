"""Importa una wallet desde su frase semilla (12/24 palabras).

    python -m server.seed

La frase se teclea sin eco, **no se guarda en ningún sitio y no se imprime**.
De ella se derivan las primeras cuentas, se te enseñan con su saldo para que
reconozcas la tuya, y sólo la que elijas acaba escrita como clave privada.

Por qué "todas sus direcciones": una clave privada de Solana *es* una única
dirección. Lo que tiene varias cuentas es la semilla: cada una se deriva en una
ruta distinta (``m/44'/501'/i'/0'``). Por eso el único modo de traerse "la
wallet entera" es desde las palabras.

Rutas que se prueban, porque no todos los monederos usan la misma:

    m/44'/501'/i'/0'   Phantom, Backpack        (lo normal)
    m/44'/501'/i'      Solflare, Ledger
    m/44'/501'/0'      la vieja de Sollet
    (raíz)             algunas herramientas usan los 32 bytes del seed tal cual

Seguridad: quien tenga la frase tiene el dinero. Se teclea en tu máquina, no
viaja a ningún sitio y este módulo no la escribe en disco ni en el log.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import sys
import unicodedata
import urllib.request
from getpass import getpass
from pathlib import Path
from typing import Any

from . import config

HARDENED = 0x80000000
LAMPORTS = 1_000_000_000
#: Cuántas cuentas se derivan por cada ruta al listar.
ACCOUNTS = 5


# --------------------------------------------------------------------------- #
# BIP39: palabras -> semilla de 64 bytes                                       #
# --------------------------------------------------------------------------- #


def _normalise(phrase: str) -> str:
    """Espacios de más, saltos de línea y mayúsculas fuera; NFKD como manda BIP39."""
    return unicodedata.normalize("NFKD", " ".join(phrase.lower().split()))


def _check_words(phrase: str) -> str | None:
    """Devuelve un aviso si la frase no valida, o None si está bien.

    El checksum de BIP39 es lo único que distingue "me he equivocado en una
    palabra" de "esta es otra wallet con saldo cero". Sin él, un typo te deja
    mirando una dirección vacía sin saber por qué.
    """
    words = phrase.split()
    if len(words) not in (12, 15, 18, 21, 24):
        return f"Una frase BIP39 tiene 12, 15, 18, 21 o 24 palabras; me has dado {len(words)}."
    try:
        from mnemonic import Mnemonic  # noqa: PLC0415
    except ImportError:
        return None  # sin la librería seguimos, pero sin red de seguridad
    if not Mnemonic("english").check(phrase):
        return (
            "El checksum no cuadra: hay alguna palabra mal escrita o en otro orden.\n"
            "    (si tu frase no está en inglés, este comprobador no la entiende)"
        )
    return None


def _seed_from_words(phrase: str, passphrase: str = "") -> bytes:
    salt = _normalise("mnemonic" + passphrase) if passphrase else "mnemonic"
    return hashlib.pbkdf2_hmac(
        "sha512", phrase.encode("utf-8"), salt.encode("utf-8"), 2048
    )


# --------------------------------------------------------------------------- #
# SLIP-0010: semilla -> claves ed25519 por ruta                                #
# --------------------------------------------------------------------------- #


def _master(seed: bytes) -> tuple[bytes, bytes]:
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def _derive(seed: bytes, path: tuple[int, ...]) -> bytes:
    """Deriva la clave privada de 32 bytes de una ruta. ed25519 sólo admite
    derivación endurecida, así que todos los índices van con el bit alto."""
    key, chain = _master(seed)
    for index in path:
        data = b"\x00" + key + (index | HARDENED).to_bytes(4, "big")
        digest = hmac.new(chain, data, hashlib.sha512).digest()
        key, chain = digest[:32], digest[32:]
    return key


def _paths() -> list[tuple[str, tuple[int, ...]]]:
    out: list[tuple[str, tuple[int, ...]]] = []
    for i in range(ACCOUNTS):
        out.append((f"m/44'/501'/{i}'/0'", (44, 501, i, 0)))
    for i in range(ACCOUNTS):
        out.append((f"m/44'/501'/{i}'", (44, 501, i)))
    return out


def _keypair_from_private(private: bytes) -> Any:
    from solders.keypair import Keypair  # noqa: PLC0415

    return Keypair.from_seed(private)


def _accounts(seed: bytes) -> list[dict[str, Any]]:
    """Todas las candidatas: cada ruta más el seed crudo, sin repetir dirección."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    entries: list[tuple[str, bytes]] = [(label, _derive(seed, p)) for label, p in _paths()]
    entries.append(("seed crudo (32 bytes)", seed[:32]))

    for label, private in entries:
        keypair = _keypair_from_private(private)
        address = str(keypair.pubkey())
        if address in seen:
            continue
        seen.add(address)
        found.append({"path": label, "address": address, "keypair": keypair})
    return found


# --------------------------------------------------------------------------- #
# Saldos (para que reconozcas cuál es la tuya)                                 #
# --------------------------------------------------------------------------- #


def _balances(addresses: list[str]) -> dict[str, float | None]:
    """Saldo en SOL de cada dirección. None si el RPC no contesta."""
    out: dict[str, float | None] = {}
    for address in addresses:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]}
        ).encode()
        request = urllib.request.Request(
            config.RPC_ENDPOINT, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                body = json.loads(response.read())
            out[address] = body["result"]["value"] / LAMPORTS
        except Exception:  # noqa: BLE001 - un RPC caído no debe abortar la importación
            out[address] = None
    return out


# --------------------------------------------------------------------------- #
# Escritura en los .env                                                        #
# --------------------------------------------------------------------------- #


def _set_env_value(path: Path, key: str, value: str, *, backup: bool = False) -> None:
    """Escribe KEY=valor en un .env, respetando el resto del fichero."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if backup and text:
        copy = path.with_suffix(path.suffix + ".bak_seed")
        copy.write_text(text, encoding="utf-8")
        _lock_down(copy)
        print(f"    copia de seguridad: {copy.name}")

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _lock_down(path)


def _lock_down(path: Path) -> None:
    """Permisos 600. En Windows es simbólico, pero en el droplet importa."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _ask(question: str) -> bool:
    return input(f"{question} [s/N]: ").strip().lower() in ("s", "si", "sí", "y", "yes")


# --------------------------------------------------------------------------- #


def main() -> int:  # noqa: PLR0911, PLR0912, PLR0915 - es un asistente lineal
    try:
        import solders  # noqa: F401, PLC0415
    except ImportError:
        print("Falta 'solders'. Instálalo:  pip install solders", file=sys.stderr)
        return 1

    print(__doc__.split("Seguridad:")[0].rstrip())
    print("=" * 74)
    print("La frase NO se ve al teclearla, NO se guarda y NO se imprime.")
    print("Si te has equivocado, Ctrl+C y vuelves a empezar.\n")

    phrase = _normalise(getpass("Frase semilla (12/24 palabras): "))
    if not phrase:
        print("No has escrito nada.", file=sys.stderr)
        return 1

    problem = _check_words(phrase)
    if problem:
        print(f"\n!! {problem}", file=sys.stderr)
        if not _ask("\n¿Sigo de todas formas?"):
            return 1

    passphrase = getpass(
        "Contraseña extra de la semilla (la '25 palabra'; Enter si no tienes): "
    )

    seed = _seed_from_words(phrase, passphrase)
    del phrase, passphrase  # fuera de memoria en cuanto deja de hacer falta

    print("\nDerivando cuentas y consultando saldos...\n")
    accounts = _accounts(seed)
    balances = _balances([a["address"] for a in accounts])

    print(f"{'#':>3}  {'RUTA':<22}  {'DIRECCIÓN':<46}  SALDO")
    print("-" * 96)
    for i, account in enumerate(accounts):
        balance = balances.get(account["address"])
        shown = "  (RPC no responde)" if balance is None else f"{balance:.4f} SOL"
        mark = " <--" if balance else ""
        print(f"{i:>3}  {account['path']:<22}  {account['address']:<46}  {shown}{mark}")

    print(
        "\nLas marcadas con <-- tienen saldo. Si ninguna es la tuya, es que la frase\n"
        "no es esa o tu monedero usa otra ruta distinta a las de arriba.\n"
    )

    raw = input("¿Qué número quieres usar para operar? (Enter para salir): ").strip()
    if not raw.isdigit() or int(raw) >= len(accounts):
        print("No se ha tocado nada.")
        return 0

    chosen = accounts[int(raw)]
    secret = chosen["keypair"].to_base58_string()
    print(f"\nElegida: {chosen['address']}  ({chosen['path']})")

    # --- panel ------------------------------------------------------------- #
    panel_env = config.APP_ROOT / ".env"
    if _ask("\n¿Que el PANEL use esta wallet (comprar/vender desde la web)?"):
        _set_env_value(panel_env, "PANEL_WALLET_SECRET", secret, backup=True)
        _set_env_value(panel_env, "PANEL_WALLET_PUBKEY", chosen["address"])
        print(f"    escrito en {panel_env}")

        print(
            "\n    Para que los botones de comprar/vender funcionen hace falta\n"
            "    PANEL_HOT_WALLET=true. Eso convierte el panel en wallet caliente:\n"
            "    quien entre puede mover este dinero. Detrás de HTTP plano, no.\n"
        )
        if _ask("    ¿Lo enciendo?"):
            _set_env_value(panel_env, "PANEL_HOT_WALLET", "true")
            print("    PANEL_HOT_WALLET=true")
        else:
            print("    lo dejo en false; puedes encenderlo luego en el .env")

    # --- bot --------------------------------------------------------------- #
    bot_env = config.BOT_REPO / ".env"
    if bot_env.exists():
        print(
            f"\n    El bot ({config.BOT_REPO}) snipea con la clave de SU .env.\n"
            "    Cambiarla hace que las próximas compras salgan de esta wallet."
        )
        if _ask("    ¿Que el BOT snipee también con esta?"):
            _set_env_value(bot_env, "SOLANA_PRIVATE_KEY", secret, backup=True)
            print(f"    escrito en {bot_env}")
            print("    reinicia el bot para que la coja")
    else:
        print(f"\n    No encuentro el .env del bot en {bot_env}; ese lo dejo como está.")

    print(
        "\nHecho. Reinicia el panel para que lea el .env nuevo.\n"
        "La frase semilla no se ha guardado: si quieres otra cuenta, vuelve a lanzarlo."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
