# morning-brief

A self-hosted, private podcast generator. It runs in one Docker container on a home server and produces two shows you can follow in Apple Podcasts:

- **Deep Dives:** ~15–20 minute episodes on topics you queue from a web page. The research and writing are done by a scheduled task in the **Claude desktop app** on your Mac, which runs on your Claude subscription, so there are **no API costs**. The server turns each script into speech and publishes it.
- **Daily Brief** (optional, off by default): a ~5 minute morning news roundup built from reputable RSS feeds. It's written by the Anthropic API, at roughly $3–4 a month with Claude Sonnet.

Speech is generated locally with [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), so no audio or text leaves your server for the voice. The phone-friendly web UI lets you queue topics, play episodes, read transcripts with source links, and mark episodes heard or delete them.

```
Web UI ── queue a topic ──► server (FastAPI + SQLite)
                              ▲  claim / submit script (token-protected API)
Claude desktop app on your Mac ┘  hourly scheduled task: web research → script
server: Kokoro speech → MP3 → private podcast feeds ──► Apple Podcasts on your phone
```

## What you need

- **A server:** a Linux machine with Docker. CPU-only is fine; it was built on a 2012 i5 with 8 GB of RAM. Speech rendering peaks around 1.7 GB of RAM, and a 20-minute episode takes about 11 minutes to render there.
- **A private network** so your phone can reach the server. [Tailscale](https://tailscale.com) is recommended, and the instructions below use it.
- **For deep dives:** the Claude desktop app on a Mac, with a Claude plan that includes scheduled tasks.
- **For the daily brief only:** an Anthropic API key.

## Quick start (server)

```bash
git clone https://github.com/cg711/morning-brief.git && cd morning-brief
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # run twice: FEED_TOKEN and WORKER_TOKEN
```

Edit `.env`:
- `FEED_TOKEN` and `WORKER_TOKEN`: the two random tokens you just generated.
- `TAILSCALE_IP`: the server's `tailscale ip -4`, or its LAN IP.
- `PUBLIC_BASE_URL`: see "Serving the feeds" below.
- `BRIEF_TZ`: your time zone.

Then start it:

```bash
mkdir -p data && docker compose up -d --build
```

The container runs as uid:gid 1000:1000 by default, not root. If your user's `id -u` differs, set `PUID` and `PGID` in `.env`. **Upgrading from an older version** that ran as root: run `sudo chown -R 1000:1000 data` once (or your `PUID:PGID`) before restarting.

Open `http://<TAILSCALE_IP>:8430`. The first episode downloads the Kokoro model (~340 MB) into `data/models/`.

> Keep the web UI on a private network (Tailscale or your LAN); don't expose port 8430 to the internet. You can also put it behind a password; see [Login and sharing](#login-and-sharing-optional).

## Serving the feeds to your phone

iOS wants HTTPS feeds. With Tailscale you get a real certificate for free:

```bash
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8430
```

Set `PUBLIC_BASE_URL=https://<machine>.<your-tailnet>.ts.net:8443` in `.env` and restart the container (`docker compose up -d`). The feed URLs appear at the bottom of the web page, each with a Copy button.

In Apple Podcasts, go to **Library → ⋯ → Follow a Show by URL**, paste the URL, then turn on **Automatically Download** in the show's settings. Apple Podcasts fetches private feeds from the phone itself, so it works over Tailscale.

Apps that fetch feeds from their own servers can't reach a tailnet address; Overcast and Pocket Casts are examples. If you need one of those, set `FUNNEL_FEEDS=1` and expose `/feed/` and `/audio/` with Tailscale Funnel. Without that setting, the app answers Funnel traffic only on share links (`/s/…`).

## Deep dives: setting up the Mac worker

1. Render the worker prompt with your server's address:

   ```bash
   python3 scripts/render_worker_prompt.py --server http://<TAILSCALE_IP>:8430
   ```

   This writes `worker/deep-dive-task.md` (gitignored) and prints the remaining steps.
2. Save the server's `WORKER_TOKEN` into the header file the script names (`~/.config/morning-brief/worker-header`, mode 600).
3. Add the printed permission rules to `~/.claude/settings.json` under `permissions.allow`. They let the unattended hourly run search and read the web and talk only to your server, without stopping for approval.
4. In the Claude desktop app, create a scheduled task that runs hourly, with the contents of `worker/deep-dive-task.md`. Queue a topic in the web UI and press **Run now** once to check it end to end.

After updating morning-brief, re-run scripts/render_worker_prompt.py and paste the new worker/deep-dive-task.md into your scheduled task: new features like links, fact-checking and two hosts need the updated prompt. Update the scheduled task before ticking Fact-check or Two hosts on any topic. An older prompt can't produce those formats: such topics fail, or claim a fact-check that never ran.

How it behaves:
- The worker runs whenever the Claude app is open. It keeps up to **3 unheard episodes** ready.
- **Mark heard** frees a slot, and so does waiting 7 days. Heard episodes are deleted after 30 days.
- An idle hourly check is a single request, so it costs next to nothing.
- **From a link:** paste a URL into the Link field. The topic is optional, and the worker builds the episode around that page.
- **Fact-check pass** and **Two hosts** are checkboxes on each topic. The "New topics default to" switches set their starting state. A fact-check costs roughly 30–50% more of your Claude usage per episode. Two-host episodes use `VOICE` for the host and `COHOST_VOICE` (default `af_heart`) for the co-host.
- **Suggested topics:** each episode proposes up to 3 related topics. Add or dismiss them from the page.

## Notifications (optional)

The server can push to your phone through [ntfy](https://ntfy.sh) when a deep dive is ready, and when research or speech is running much longer than usual (research over 60 minutes, speech over 40).

1. Generate a topic name: `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'`. It's the only secret, so keep it long and random.
2. Put it in `.env` as `NTFY_TOPIC` and restart the container.
3. Install the ntfy app on your phone and subscribe to that topic on `ntfy.sh`.

With the public ntfy.sh server, episode titles and topic names pass through ntfy.sh. To avoid that, run your own ntfy server and set `NTFY_SERVER`.

The web page also shows when the Mac worker last checked in. The line turns amber after 2 hours of silence.

## Daily brief (optional)

Two ways to have a daily ~5-minute news brief:

- **`DAILY_BRIEF=worker` (no API cost):** at `RUN_AT` the server gathers candidate stories from `feeds.yaml`. A second scheduled task on your Mac (`daily-brief-task.md` from `scripts/render_worker_prompt.py`) picks 6–8 stories, reads them and writes the script, and the server checks, speaks and publishes it.
  - Run that task shortly after `RUN_AT`, with a retry before `READY_BY`. For example, cron `5,45 7,8 * * *` for `RUN_AT=07:30` and `READY_BY=08:30`.
  - The Mac must be awake with the Claude app open. If there's no brief by `READY_BY`, you get an ntfy push instead.
  - Regenerate re-gathers; press Run now on the daily task to write it straight away.
- **`DAILY_BRIEF=1`:** the Anthropic API writes it at `RUN_AT` (retry at `RETRY_AT`), roughly $3–4 a month with Sonnet. Needs `ANTHROPIC_API_KEY`.

Either way, pick your `LISTENER_LOCATION` and replace the `local` feeds in `feeds.yaml`, which default to Minneapolis. Each brief cites only stories from your feeds, names its sources out loud, and is checked before speaking: 300–750 words, and every story must cite a real item.

## Personal segment (optional)

In worker mode the daily brief can open with a short "Your morning" segment: last night's sleep and readiness and today's tip from an Oura dashboard service (`OURA_DASHBOARD_URL`, which must serve `GET /api/state`), and yesterday's spending from Actual Budget (`ACTUAL_SERVER_URL`, `ACTUAL_PASSWORD`, `ACTUAL_SYNC_ID`; see `.env.example`). Turn it on with `PERSONAL_SEGMENT=1`, then check the connections with:

    docker compose exec morning-brief python -m morning_brief.personal --check

Each source is optional; if one can't be reached, that part is simply left out. Privacy: these numbers and payee names go to your Mac worker's Claude session and into the episode audio and transcript on your own server. It isn't included in the podcast feed's episode notes. The Mac keeps the latest claim (with these facts) in its cache folder, and each morning's run overwrites it.

It also covers today's **weather** (from [Open-Meteo](https://open-meteo.com), free and keyless, for `LISTENER_LOCATION`; set `WEATHER_LAT`/`WEATHER_LON` if the name resolves to the wrong place) and today's **calendar**. The calendar comes from `GET {OURA_DASHBOARD_URL}/api/agenda?date=YYYY-MM-DD`. Any service can provide it by answering `{"date", "connected": true, "events": [{"title", "start", "end", "all_day", "calendar"}]}`, with ISO times that include an offset. Up to five upcoming events and three all-day items are read out. Event titles, like payee names, go to your Mac worker's Claude session and into the episode audio and transcript.

## Send from your phone (optional)

Share a link or some text from any iPhone app to a **Morning Brief** Shortcut. It asks for an optional note and whether to put the topic at the top or the end of the queue, then adds a deep dive with your page defaults. Set `INBOX_TOKEN`, then follow [shortcuts/README.md](shortcuts/README.md). The endpoint is `POST /api/inbox`; like the rest of `/api/`, it's reachable only on your tailnet and never through Funnel.

## Login and sharing (optional)

**Login.** Set `UI_PASSWORD` in `.env` and restart. The web UI then asks for the password once per device and remembers it with a signed cookie for `SESSION_DAYS` days (default 90).
- Changing the password logs every device out.
- After five wrong passwords in ten minutes, logins pause until ten minutes have passed.
- The podcast feeds, audio, share links and the Mac worker's API keep their own tokens and don't need the login.
- The signing secret is created in `data/session_secret`.
- Logging out only clears that browser's cookie. To log out every device at once, change the password or delete `data/session_secret` and restart.

**Share links.** You can share a single deep dive with someone who isn't on your tailnet.
1. Turn on [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) for your server. You may first need to allow Funnel for the machine in your tailnet policy. Then expose only the share path:

   ```bash
   sudo tailscale funnel --bg --set-path /s/ http://127.0.0.1:8430/s/
   ```

2. Set `SHARE_BASE_URL` to the machine's Funnel address, e.g. `https://myserver.tailnet-name.ts.net`, and restart.
3. Ready and heard deep dives get a **Share** button. It creates a link to a small page with the player, chapters and transcript. The page has no way back into the app.

What to expect:
- A link lasts until you press **Revoke link**, or until you delete the episode.
- Shared episodes are never auto-deleted.
- Daily briefs can't be shared.
- Share pages ask search engines not to index them.
- The app refuses every non-`/s/` path for Funnel traffic, even if Funnel is set up more broadly.
- Unsetting `SHARE_BASE_URL` hides the Share button but doesn't kill existing links; revoke them first, or remove the Funnel mapping.

## Customizing

| Setting | Where | Notes |
|---|---|---|
| Voice | `VOICE` in `.env` | Any Kokoro voice (`am_michael`, `af_heart`, `af_bella`, `bf_emma`, …) |
| Co-host voice | `COHOST_VOICE` in `.env` | Used by two-host episodes (default `af_heart`) |
| Speaking pace | `SPEED` in `morning_brief/speech.py` | Default 1.1× |
| Intro/outro music | `data/music/intro.wav`, `outro.wav`; `MUSIC=0` | A generated sting by default. Your own PCM WAVs, up to 30 s, replace it |
| News sources | `feeds.yaml` | Grouped by segment: `headlines`, `tech`, `business`, `local` |
| Schedule and time zone | `RUN_AT`, `RETRY_AT`, `BRIEF_TZ` | Daily brief only |

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

- Kokoro's bundled espeak doesn't run on macOS. Set `FAKE_SPEECH=1` for silent placeholder audio on a Mac, and run the real-speech test inside the container:

  ```bash
  docker compose run --rm -v "$PWD":/work -w /work morning-brief sh -c "pip install -q --user pytest mutagen && python -m pytest -q -p no:cacheprovider -m container"
  ```

- `CLAUDE_OFFLINE=1` swaps the Anthropic API for a deterministic local stand-in. It lets you run the whole daily pipeline for free; it's a plumbing check, not a quality check.

## Credits

- **Speech:** [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0) via [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (MIT). The model is downloaded at runtime, not included here.
- **Web UI:** [htmx](https://htmx.org) (Zero-Clause BSD), vendored in `morning_brief/static/`. See `THIRD_PARTY_NOTICES.md`.
- **News content** belongs to its publishers. Episodes summarize and attribute stories for personal listening; they don't reproduce articles.
- Built with [Claude Code](https://claude.com/claude-code).

## License

MIT. See `LICENSE`.
