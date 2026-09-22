# UniFi Talk Alarm Add-on

This Home Assistant add-on registers a dedicated SIP extension with UniFi Talk
and places outbound alarm calls. It is the local SIP/media companion for the
`UniFi Talk Alarm` custom integration and implements its authenticated HTTP API
v1.

## Features

- dedicated UniFi Talk SIP registration through baresip
- outbound calls to explicitly allowlisted extensions or phone-number prefixes
- locally spoken `message` text with `espeak-ng`
- alternative allowlisted HTTP(S) WAV source
- `ffmpeg` normalization to PCM signed 16-bit, mono, 8 kHz
- alarm audio starts only after `CALL_ESTABLISHED`
- automatic hangup on audio EOF, with duration-plus-buffer fallback
- one call at a time, bounded ring timeout and outbound rate limiting
- inbound calls are rejected; the SIP identity is outbound-only
- hard blocking of 110, 112, 911 and 999, including common German
  international, formatted, star and hash bypasses
- configurable additional number blocks
- constant-time bearer-token authentication
- HTTP API bound only to the Home Assistant host loopback interface
- bounded JSON, download size, WAV duration and subprocess execution
- exact audio-host allowlist, no redirects and no proxy-environment inheritance
- normalized registration/call state for Home Assistant
- no SIP or bearer credentials in API status responses
- received audio is written only below `/tmp` and deleted after every call

`message` is synthesized locally. No cloud TTS service and no Piper service are
used. The MVP is outbound-only and does not queue or automatically retry calls.

## Architecture

```text
Home Assistant automation
        |
        | authenticated HTTP API v1
        v
UniFi Talk Alarm add-on
        |  espeak-ng -> ffmpeg -> WAV
        |  baresip SIP/RTP
        v
UniFi Talk -> phone or configured outbound route
```

The add-on uses host networking because SIP and RTP must advertise a reachable
address. Its authenticated HTTP API deliberately binds only to `127.0.0.1` on
the configured port (default `8099`). Configure the companion integration with
`http://127.0.0.1:8099`. LAN clients cannot reach this API; do not replace the
loopback address with the Home Assistant LAN address or the add-on slug.

## Installation and configuration

See [INSTALLATION.md](INSTALLATION.md). The add-on store also displays
[unifi_talk_alarm/DOCS.md](unifi_talk_alarm/DOCS.md).

### Guided UniFi Talk copy/paste

Only copy the four credentials from **Talk > Phones > Add Third-Party Device >
Overview**, in the order shown there:

1. In UniFi Talk, open **Talk > Phones** and select **Add Third-Party Device**.
2. Open **Overview** for that dedicated device.
3. Copy the following four fields from top to bottom and paste each value into
   the matching add-on option.
4. Create `api_token` yourself; it is not supplied by UniFi Talk.

| UniFi Talk Overview | Add-on Configuration |
| --- | --- |
| SIP Server Host | SIP Server Host |
| SIP Server Port | SIP Server Port |
| SIP Username | SIP Username |
| SIP Password | SIP Password |

Do not copy values from the SIP Trunk Provider form. Its **Provider**, **Auth
Username**, **Password**, **Outbound Number Format**, **Phone Numbers**, and
advanced **SIP Proxy**, **Realm**, **Dialplan Context**, **Register with
Provider**, **Registration Expiry**, and **IP Address Range** settings remain in
UniFi Talk. In particular, **Auth Username** is not **SIP Username**, the
provider **Password** is not **SIP Password**, and neither **SIP Proxy** nor
**Realm** belongs in `outbound_proxy`.

Important options:

| Option | Purpose |
| --- | --- |
| `sip_server` | **SIP Server Host** copied from the Third-Party Device Overview |
| `sip_port` | **SIP Server Port** copied from the Third-Party Device Overview |
| `sip_extension` | **SIP Username** copied from the Third-Party Device Overview |
| `sip_password` | **SIP Password** copied from the Third-Party Device Overview; bare account-syntax delimiters are rejected |
| `sip_transport` | Keep `udp` unless separate, verified Third-Party Device instructions require `tcp` or `tls`; TLS verifies the server certificate |
| `outbound_proxy` | Optional complete `sip:`/`sips:` device proxy URI; never copy a provider SIP Proxy or Realm |
| `api_token` | Create this private bearer token yourself; it is not a Talk value; use 32–512 non-whitespace characters |
| `api_port` | Loopback TCP port for the local HTTP API, default `8099` |
| `allowed_number_rules` | Explicit exact/prefix rules; no unsafe regular expressions |
| `blocked_number_rules` | Extra deny rules; deny always wins |
| `allowed_audio_hosts` | Exact hosts allowed for `audio_url` downloads |
| `tts_voice`, `tts_speed` | Local eSpeak voice and speaking speed |
| `max_calls_per_window` | Calls permitted per rate-limit window |
| `rate_limit_window_seconds` | Sliding in-memory rate-limit window |
| `max_audio_bytes`, `max_audio_seconds` | Remote WAV safety limits |
| `hangup_buffer_seconds` | Fallback delay after calculated WAV duration |
| `terminal_state_seconds` | Time a terminal call result remains visible before reset |
| `log_level` | `debug`, `info`, `warning`, or `error` |

Number rules are deliberately small and predictable:

- `exact:150` permits only extension `150`.
- `prefix:15` permits every number starting with `15`.
- `prefix:+49` permits numbers beginning with `+49`.
- `*` permits all syntactically valid destinations, except blocked numbers.

Emergency destinations 110, 112, 911 and 999 remain blocked independently of
the editable list. The German variants `+49110`, `+49112`, `0049110` and
`0049112` are also blocked. You are responsible for adding all other emergency,
premium-rate and prohibited numbers relevant to your country and Talk routes.

## API v1

Every request requires `Authorization: Bearer <api_token>` and uses `/api/v1`:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | authentication and API-major check |
| `GET` | `/status` | SIP registration and current call state |
| `POST` | `/calls` | prepare audio and place one call |
| `POST` | `/calls/{call_id}/hangup` | stop the matching active call |
| `POST` | `/refresh` | refresh the SIP registration view |

The complete request/response schema is defined by the companion integration's
`API_CONTRACT.md`. A call request may spend time downloading/synthesizing and
normalizing audio before it returns. Clients should allow 60 seconds, must not
retry an uncertain `POST /calls`, and should inspect `/status` instead.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements_test.txt
pytest
```

Tests use a fake SIP adapter. No real call is placed. The Docker build also
fails early unless baresip, the media tools and all required account, control,
codec, menu and file-audio modules are installed.

## Safety boundary

This project augments sirens and push notifications. It is not a certified
alarm-transmission or emergency-calling system. Test only a harmless internal
extension first. The API is loopback-only; never expose SIP or RTP directly to
the public internet.
