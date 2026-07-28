"""Generate a PANEL_PASSWORD_HASH value.

    python -m server.hashpw

The password is read without echo and never written anywhere -- copy the printed
line into the panel's .env and delete any plaintext PANEL_PASSWORD.
"""

from __future__ import annotations

import getpass
import sys

from .auth import hash_password


def main() -> int:
    first = getpass.getpass("Contrasena del panel: ")
    if len(first) < 12:
        print("Usa al menos 12 caracteres: esto es la puerta de un wallet.", file=sys.stderr)
        return 1
    if first != getpass.getpass("Reptela: "):
        print("No coinciden.", file=sys.stderr)
        return 1
    print("\nPega esto en el .env del panel:\n")
    print(f"PANEL_PASSWORD_HASH={hash_password(first)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
