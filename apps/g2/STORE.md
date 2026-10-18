# Even Hub beta / store release

Rules: <https://hub.evenrealities.com/docs/ship/app-submission>. Dev (QR sideload via `./scripts/dev.sh`) is
unaffected by anything here.

## Build

```bash
uv run bluebook pack                                  # talks to this laptop's tailnet server
uv run bluebook pack --origin https://<public-server> # talks to a hosted server (none exists yet)
cd apps/g2 && pnpm exec evenhub login                 # once, then add --check to validate the package id
```

Output: `apps/g2/bluebook-<host>-<version>.ehpk`. The server origin is baked in at build time and is the
only network whitelist entry. Each upload needs a higher `version` in `app.json` (released versions are permanent).

## Upload

1. <https://evenhub.evenrealities.com> → upload the `.ehpk` (Draft).
2. Move to **Test**, install as a private build / assign a beta group.
3. Only move to **Submitted** once the checks below pass. That step is one-way.

## Checks before submitting (beta build, not QR sideload)

- [ ] Phone locked + Even app backgrounded 5 min: the lesson resumes on the same step.
- [ ] Double-tap on the root page shows the system exit dialog; the WebView closes; Conversate then launches.
- [ ] Server down: glasses show "can't reach its server", double-tap still exits (no black screen).
- [ ] Idle 2 min, then input still works.

## Listing assets (portal)

- **Icon:** greyscale foreground + background (colour is rejected). Not made yet.
- **Screenshots:** from the simulator (`./scripts/dev.sh --sim`, screenshot button). Must match the device.
- **Name:** `bluebook` (must equal `app.json` and the on-glasses name; ≤20 chars, no "Even").
- **Description:** bluebook teaches hands-on tasks step by step on your glasses. Follow each step, hold the
  touchpad to ask the tutor a question by voice, and move on when you're ready.
- **Release notes (0.1.0):** Learn a hands-on task one step at a time on your glasses, and hold the touchpad
  to ask the tutor a question.
- **Privacy policy** (must cover every permission and name the backend domain):
  - *Network:* connects only to the bluebook server at the origin in the build, to load lessons and get guidance.
  - *Microphone:* records only while you hold the touchpad; audio goes to that server, which transcribes it with
    Google Gemini to answer your question. Audio isn't stored on the glasses or phone.
  - Glasses serial number (or a random phone id) is sent to resume your lesson after a reconnect.
