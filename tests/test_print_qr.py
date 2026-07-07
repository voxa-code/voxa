from scripts.print_qr import pairing_url


def test_pairing_url():
    assert pairing_url("mac.tail.ts.net", "abc") == "https://mac.tail.ts.net/?token=abc"
