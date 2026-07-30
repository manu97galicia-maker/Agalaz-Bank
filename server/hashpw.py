"""Generate a PANEL_PASSWORD_HASH (or PANEL_PIN_HASH) value.

    python -m server.hashpw          # contraseña larga
    python -m server.hashpw --pin    # PIN corto de 4+ dígitos

The secret is read without echo and never written anywhere -- copy the printed
line into the panel's .env and delete any plaintext value.
"""

from __future__ import annotations

import getpass
import sys

from .auth import hash_password


def main() -> int:
    pin_mode = "--pin" in sys.argv
    label = "PIN" if pin_mode else "Contrasena del panel"
    min_len = 4 if pin_mode else 12

    first = getpass.getpass(f"{label}: ")
    if len(first) < min_len:
        kind = "digitos" if pin_mode else "caracteres"
        print(f"Usa al menos {min_len} {kind}: esto es la puerta de un wallet.", file=sys.stderr)
        return 1
    if first != getpass.getpass("Repitelo: "):
        print("No coinciden.", file=sys.stderr)
        return 1

    var = "PANEL_PIN_HASH" if pin_mode else "PANEL_PASSWORD_HASH"
    print("\nPega esto en el .env del panel:\n")
    print(f"{var}={hash_password(first)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
