# Changelog

## 0.1.0

- Initial authenticated local API v1 service.
- Added baresip registration and outbound single-call state machine.
- Added local eSpeak TTS and allowlisted WAV downloads.
- Added call allow/deny policy, emergency blocking and rate limiting.
- Fixed baresip account/control module loading and mono audio configuration.
- Enforced an outbound-only single-call policy and serialized dial/hangup.
- Bound the authenticated API to loopback and strengthened API-token validation.
- Expanded emergency aliases and made received audio temporary with cleanup.
- Migrated packaging to the multi-architecture Home Assistant Debian base image.
- Added guided German and English configuration labels that mirror the four
  Third-Party Device fields and warn against SIP Trunk Provider credentials.
