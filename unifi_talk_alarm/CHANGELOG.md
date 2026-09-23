# Changelog

## 0.2.3

- Restore Baresip's persistent silence source before every dial so a second
  call cannot reuse the first call's deleted temporary alarm WAV.
- Abort a call when Baresip reports that an audio-source switch failed, even
  when its control response otherwise reports success.
- Reject audio-source paths that exceed Baresip's device field instead of
  silently truncating them.
- Classify missing-file call closures and WAV-open warnings without printing
  the path or the raw Baresip output.

## 0.2.2

- Added privacy-safe categories for Baresip call-close reasons, SDP/audio
  warnings and normal WAV end-of-file events to help diagnose silent calls.
- Report when Baresip returns an audio-source switch failure inside an otherwise
  successful control response, without logging its raw response or file path.

## 0.2.1

- Added privacy-safe SIP event and command-result diagnostics to the add-on log,
  including a SIP status code on call closure when available.
- Parse structured Baresip call events by exact event type so a call-close reason
  cannot be mistaken for a connected call.

## 0.2.0

- Added bounded `audio_wav_base64` call input for WAV audio rendered by Home
  Assistant, including strict Base64, request-size, PCM WAV, byte and duration
  validation before dialing.
- Kept the existing local `message` and allowlisted `audio_url` inputs for API
  v1 compatibility.

## 0.1.3

- Fixed alarm audio ending before playback by distinguishing Baresip's WAV
  preload marker from the actual end-of-file event.

## 0.1.2

- Fixed Baresip module discovery on the Debian add-on image so the SIP client,
  registration, and local control channel can start.

## 0.1.1

- Added guided German and English configuration labels that mirror the four
  Third-Party Device fields and warn against SIP Trunk Provider credentials.

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
