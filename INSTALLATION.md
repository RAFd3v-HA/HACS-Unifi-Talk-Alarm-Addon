# Installation

## 1. Create the Talk extension

Use the dedicated Third-Party Device screen; do not use the SIP Trunk Provider
screen:

1. In UniFi Talk, open **Talk > Phones**.
2. Select **Add Third-Party Device** and create a dedicated device for Home
   Assistant.
3. Open its **Overview**.
4. Copy the four displayed values from top to bottom and paste them into the
   matching add-on fields later in step 3.

| UniFi Talk Overview | Add-on Configuration |
| --- | --- |
| SIP Server Host | SIP Server Host |
| SIP Server Port | SIP Server Port |
| SIP Username | SIP Username |
| SIP Password | SIP Password |

These are the only values to copy from UniFi Talk. If the screen instead shows
**Provider**, **Auth Username**, **Password**, **Outbound Number Format**,
**Phone Numbers**, or the advanced fields **SIP Proxy**, **Realm**, **Dialplan
Context**, **Register with Provider**, **Registration Expiry**, and **IP Address
Range**, you are configuring a SIP trunk and are on the wrong screen. Those
values stay in UniFi Talk. **Auth Username** is not **SIP Username**, the
provider **Password** is not **SIP Password**, and **SIP Proxy** or **Realm**
must not be pasted into `outbound_proxy`.

Do not reuse a console administrator account. The add-on does not need a UniFi
console login, email, MFA code or API key.

## 2. Add the add-on repository

The add-on is required. The Home Assistant integration is only the control and
entity layer; it cannot register the SIP extension or play call audio itself.

1. Open **Settings → Add-ons → Add-on Store** in Home Assistant.
2. Open **⋮ → Repositories**.
3. Enter
   `https://github.com/RAFd3v-HA/HACS-Unifi-Talk-Alarm-Addon`.
4. Select **UniFi Talk Alarm Add-on** and install it.

## 3. Configure the add-on

Before starting it, paste **SIP Server Host**, **SIP Server Port**, **SIP
Username**, and **SIP Password** into the four mapped fields from step 1. Keep
their spelling and values exactly as shown in the Third-Party Device Overview.

Create `api_token` yourself. It is a private credential between the add-on and
the Home Assistant integration and does not come from UniFi Talk. For example,
generate it on a trusted machine with:

```bash
openssl rand -hex 32
```

Keep `sip_transport` at `udp` and `outbound_proxy` empty unless separate,
verified Third-Party Device instructions explicitly require different values.
Never derive either option from the SIP Trunk Provider screen.

Set a narrow number policy for the first test, such as:

```yaml
allowed_number_rules:
  - exact:150
blocked_number_rules:
  - exact:110
  - exact:112
  - exact:911
  - exact:999
```

The built-in emergency blocks cannot be bypassed by removing them from the
editable option. They include 110, 112, 911, 999 and the German international
forms `+49110`, `+49112`, `0049110` and `0049112`. Add every further emergency
or prohibited destination used in your country. A deny rule always wins over
an allow rule.

`api_token` must contain at least 32 non-whitespace characters. The 64-character
hex value from the command above meets this requirement.

For a Home Assistant-hosted WAV such as
`http://homeassistant.local:8123/local/alarm.wav`, keep
`homeassistant.local` in `allowed_audio_hosts`. If your automation uses the
Home Assistant IP in the URL, add that exact IP as another list item. Do not
include a scheme, port or path in an audio-host item.

Start the add-on and check its log. It should report that API v1 is listening
and the integration should later show registration state `registered`. Logs
never intentionally print credentials; nevertheless, do not publish complete
debug logs without reviewing them.

When `sip_transport` is `tls`, the add-on verifies the Talk server certificate
against the system CA store. Use a hostname covered by a trusted certificate;
do not work around a certificate failure by exposing the API or weakening the
number policy.

## 4. Connect the Home Assistant integration

Because both Home Assistant Core and this host-network add-on share the host
network, configure the integration with the loopback URL:

```text
http://127.0.0.1:8099
```

Do not use the LAN IP, `homeassistant.local`, or `unifi-talk-alarm`. The API
listens only on loopback so another device on the LAN cannot issue calls.

Enter the exact API token from the add-on options without the word `Bearer`.
Port `8099` must be unused on the host.

## 5. Safe first call

Run a call to the harmless internal extension allowlisted above:

```yaml
action: unifi_talk_alarm.call
data:
  number: "150"
  message: "Dies ist ein Testanruf von Home Assistant."
  ring_timeout: 20
```

Expected state sequence:

```text
dialing → ringing → connected → ending → ended → idle
```

The add-on synthesizes `message` locally, converts it to 16-bit mono 8 kHz PCM,
and switches baresip to that WAV only after Talk reports `CALL_ESTABLISHED`.
It hangs up at audio EOF or after the WAV duration plus the configured buffer.
The SIP identity is outbound-only. Any received audio is temporarily directed
to `/tmp` and removed when the call ends or the add-on stops.

Do not automatically retry a call action after an HTTP timeout: the first call
may already be in progress. Check the call-state sensor or `/api/v1/status`.

## Troubleshooting

- **Health works but registration is `error`:** verify SIP host, extension,
  password, transport and optional proxy against the third-party device in
  Talk. Confirm that the Home Assistant host can reach Talk.
- **Integration cannot connect:** use `http://127.0.0.1:<api_port>`, confirm the
  add-on is running, and check its log for a port conflict or invalid options.
- **401 unauthorized:** copy `api_token` exactly; do not prefix it with
  `Bearer` in the integration field.
- **422 number_not_allowed:** add a narrow exact or prefix allow rule. Emergency
  blocks remain intentional.
- **422 audio_url_not_allowed:** add only the exact URL hostname to
  `allowed_audio_hosts`; redirects are intentionally refused.
- **429 rate_limited:** wait for `rate_limit_window_seconds`. The add-on never
  queues the call.
- **Call connects but no message is audible:** inspect the add-on log for the
  `CALL_ESTABLISHED`/audio-source transition and verify that Talk permits the
  negotiated G.711 media path and host RTP traffic.
- **TLS registration fails:** use the Talk hostname from its trusted
  certificate, verify the system clock and confirm that the configured port is
  the TLS SIP endpoint.
