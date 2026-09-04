# UniFi Talk Alarm Add-on

This add-on registers a dedicated third-party SIP extension with UniFi Talk.
The companion Home Assistant integration sends authenticated call requests to
the add-on's local API.

## Required configuration

Enter the SIP host, port, extension, password and transport shown for the
third-party device in Talk. Set a unique `api_token`; it must contain 32–512
non-whitespace characters and a 64-character random hex token is recommended.

The add-on uses host networking for SIP/RTP. Its authenticated API listens only
on host loopback at `127.0.0.1` and `api_port` (default `8099`). Configure the
integration with `http://127.0.0.1:8099`; LAN IPs, mDNS names and the add-on
slug cannot reach the loopback-only API.

When `sip_transport` is `tls`, the SIP server certificate is checked against
the system CA store. Configure the Talk hostname covered by that certificate.

## Options

| Option | Meaning |
| --- | --- |
| `sip_server`, `sip_port` | Talk SIP hostname/address and port |
| `sip_extension`, `sip_password` | Dedicated third-party SIP credentials |
| `sip_transport` | `udp`, `tcp`, or certificate-verified `tls` |
| `outbound_proxy` | Optional complete `sip:` or `sips:` proxy URI |
| `api_token`, `api_port` | Shared bearer token and loopback API port |
| `allowed_number_rules` | Exact/prefix outbound allow rules |
| `blocked_number_rules` | Additional deny rules; deny wins |
| `allowed_audio_hosts` | Exact host allowlist for remote WAV files |
| `tts_voice`, `tts_speed` | Local eSpeak voice and speed |
| `max_calls_per_window`, `rate_limit_window_seconds` | Sliding outbound rate limit |
| `max_audio_bytes`, `max_audio_seconds` | Remote/media processing limits |
| `hangup_buffer_seconds` | Audio-duration fallback buffer |
| `terminal_state_seconds` | Terminal result display time |
| `log_level` | `debug`, `info`, `warning`, or `error` |

## Number safety

`allowed_number_rules` supports exact entries (`exact:150`), prefix entries
(`prefix:+49`) and `*`. `blocked_number_rules` uses the same syntax and always
wins. 110, 112, 911 and 999 are additionally hard-blocked after dial-string
normalization, including `+49110`, `+49112`, `0049110` and `0049112`. Add the
other emergency/premium destinations relevant to your country; that
responsibility remains with the administrator.

## Audio

Supplying `message` uses local `espeak-ng`; `tts_voice` defaults to `de`.
Supplying `audio_url` instead downloads one uncompressed PCM WAV from an exact
`allowed_audio_hosts` entry. Redirects, URL credentials and oversized or long
WAV files are rejected. `ffmpeg` produces a mono 8 kHz signed 16-bit PCM WAV.

Alarm audio is never selected as the SIP source before `CALL_ESTABLISHED`.
After EOF the call is ended; WAV duration plus `hangup_buffer_seconds` is the
fallback when baresip emits no EOF event.

## Limits

Only one outbound call can be active. Calls are not queued or retried. The
sliding rate limit defaults to three successfully prepared dial attempts per
five minutes. Inbound calls are rejected. Received call audio is written only
to a temporary file below `/tmp` and deleted after the call or add-on shutdown;
there is no recording feature. The MVP has no DTMF or voicemail feature.

The API is loopback-only. Never expose SIP or RTP to the public internet. This
add-on is not a certified alarm or emergency-call system.
