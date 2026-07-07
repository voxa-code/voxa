from __future__ import annotations

import sys
import qrcode


def pairing_url(dns_name: str, token: str) -> str:
    return f"https://{dns_name}/?token={token}"


def print_qr(text: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(text)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


if __name__ == "__main__":
    url = sys.argv[1]
    print_qr(url)
    print(url)
