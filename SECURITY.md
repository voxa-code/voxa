# Security

## Reporting a vulnerability

Email security concerns to security@voxa.space (or open a private security advisory
on the GitHub repo). Please do not file public issues for exploitable bugs.

## How Voxa is secured

- **No user API keys on the laptop.** In the hosted model the cloud holds the
  Gemini/APNs keys; the laptop and phone pair with a per-machine token.
- **Purchases are verified.** StoreKit transactions are checked with a mandatory
  Apple certificate-chain verification that fails closed. Apple's root
  ("Apple Root CA - G3") ships in `server/certs/`; override with `APPLE_ROOT_CERT`.
- **Transport is TLS everywhere** (https/wss). The iOS app has no ATS exceptions;
  the pairing URL is upgraded to https at parse time.
- **Secrets stay out of the repo and packages.** Runtime stores
  (`users.json`, `billing.json`, `devices.json`, `waitlist.jsonl`) and `.env`
  files are gitignored and excluded from the npm/PyPI artifacts. iOS secrets live
  in the Keychain (`ThisDeviceOnly`, kept out of backups).
- **Push/call routes are ACCOUNT-scoped.** Registration and ring/cancel are keyed
  by an unguessable 128-bit account id, not a shared secret (the hosted client
  intentionally holds no server secret).

## Known limitations / hardening roadmap

These are accepted tradeoffs of the current zero-config design, tracked for a
future authenticated-account model:

- **Anonymous device accounts are self-asserted.** A new `d-` account gets free
  trial minutes on first contact. Two layers protect this: (1) creation of new
  `d-` accounts is rate-limited per IP + globally (always on), and (2) real Apple
  **App Attest** binds an account to a physical Secure Enclave device
  (`server/appattest.py`, iOS `AppAttest.swift`), behind
  `VOXA_REQUIRE_ATTESTATION` (default OFF). When enabled, an unattested `d-`
  account gets no free trial and cannot open `/live`. Enable it once the attested
  app build is live, after confirming attestation on a real device.
- **The metered `/live` proxy**: with attestation enabled only attested accounts
  can open a session, which closes the free-Gemini vector.
- **Pairing trust.** Whoever holds the pairing code+URL can drive the paired
  laptop; keep the QR/URL private. Codes are 128-bit; do not paste pairing URLs
  into shared/logged locations.
- **Session tokens** are long-lived (1 year) with no server-side revocation yet.
  Use a strong `VOXA_AUTH_SECRET` (>= 32 random chars); the server refuses to
  start on an obviously-weak one.

## Before going fully public

Run a full git-history secret scan (`gitleaks detect` or
`trufflehog git file://.`) as insurance, even though the working tree is clean.
