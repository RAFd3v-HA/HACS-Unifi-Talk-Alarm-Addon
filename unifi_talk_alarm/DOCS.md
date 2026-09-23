# UniFi Talk Alarm Add-on

This add-on registers a dedicated third-party SIP extension with UniFi Talk.
The companion Home Assistant integration sends authenticated call requests to
the add-on's local API.

## Required configuration

Use this guided copy/paste flow:

1. In UniFi Talk, open **Talk > Phones**.
2. Select **Add Third-Party Device**, then open that device's **Overview**.
3. Copy the four fields from top to bottom into the mapped add-on options.
4. Create `api_token` yourself; do not look for it in UniFi Talk.

| UniFi Talk Overview | Add-on Configuration |
| --- | --- |
| SIP Server Host | SIP Server Host |
| SIP Server Port | SIP Server Port |
| SIP Username | SIP Username |
| SIP Password | SIP Password |

Do not copy anything from the SIP Trunk Provider form. **Provider**, **Auth
Username**, **Password**, **Outbound Number Format**, **Phone Numbers**, and the
advanced **SIP Proxy**, **Realm**, **Dialplan Context**, **Register with
Provider**, **Registration Expiry**, and **IP Address Range** settings remain in
UniFi Talk. **Auth Username** is not **SIP Username**, the provider **Password**
is not **SIP Password**, and **SIP Proxy** or **Realm** must not be entered as
`outbound_proxy`.

Set a unique `api_token`; it must contain 32–512 non-whitespace characters and
a 64-character random hex token is recommended. The token is created by you,
is shared only with the Home Assistant integration, and is not a Talk value.

The add-on uses host networking for SIP/RTP. Its authenticated API listens only
on host loopback at `127.0.0.1` and `api_port` (default `8099`). Configure the
integration with `http://127.0.0.1:8099`; LAN IPs, mDNS names and the add-on
slug cannot reach the loopback-only API.

When `sip_transport` is `tls`, the SIP server certificate is checked against
the system CA store. Configure the Talk hostname covered by that certificate.

## Options

| Option | Meaning |
| --- | --- |
| `sip_server` | **SIP Server Host** from the Third-Party Device Overview |
| `sip_port` | **SIP Server Port** from the Third-Party Device Overview |
| `sip_extension` | **SIP Username** from the Third-Party Device Overview |
| `sip_password` | **SIP Password** from the Third-Party Device Overview |
| `sip_transport` | Keep `udp` unless separate, verified Third-Party Device instructions require `tcp` or certificate-verified `tls` |
| `outbound_proxy` | Optional complete Third-Party Device `sip:` or `sips:` proxy URI; never a provider SIP Proxy or Realm |
| `api_token` | Private token you create yourself; it does not come from Talk |
| `api_port` | Loopback API port used by the Home Assistant integration |
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
WAV files are rejected. The companion integration may alternatively submit a
Home Assistant-rendered WAV as bounded `audio_wav_base64`; only the audio, not
the Home Assistant Cloud credential, reaches this add-on. Exactly one of the
three sources is accepted. Base64 syntax, request size, decoded size and PCM
WAV structure are checked before dialing. `ffmpeg` produces a mono 8 kHz signed
16-bit PCM WAV.

Alarm audio is never selected as the SIP source before `CALL_ESTABLISHED`.
After EOF the call is ended; WAV duration plus `hangup_buffer_seconds` is the
fallback when baresip emits no EOF event.

## Call diagnostics

With `log_level: info`, the add-on's **Log** tab shows the ordered Baresip call
events (`CALL_RINGING`, `CALL_ANSWERED`, `CALL_ESTABLISHED`, `CALL_CLOSED`),
media events (`CALL_RTPESTAB`, `AUDIO_ERROR`), and, when available, a
three-digit `sip_code` on closure. `CALL_ANSWERED` alone does not start alarm
audio; playback begins only after `CALL_ESTABLISHED`. The log does not include
numbers, SIP URIs, passwords, API tokens, or raw SIP reasons. For a failed
test call, copy the lines beginning `Baresip call event`, `Baresip media event`
and `Baresip command` from that test only.
`AUDIO_ERROR` can also occur when WAV playback reaches its normal end, so its
presence alone does not prove an audio fault.

## Limits

Only one outbound call can be active. Calls are not queued or retried. The
sliding rate limit defaults to three successfully prepared dial attempts per
five minutes. Inbound calls are rejected. Received call audio is written only
to a temporary file below `/tmp` and deleted after the call or add-on shutdown;
there is no recording feature. The MVP has no DTMF or voicemail feature.

The API is loopback-only. Never expose SIP or RTP to the public internet. This
add-on is not a certified alarm or emergency-call system.
