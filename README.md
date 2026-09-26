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

Open `http://<TAILSCALE_IP>:8430`. The first episode downloads the Kokoro model (~340 MB) into `data/models/`.

> The web UI has no login. Keep it on a private network (Tailscale or your LAN); don't expose port 8430 to the internet.

## Serving the feeds to your phone

iOS wants HTTPS feeds. With Tailscale you get a real certificate for free:

```bash
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8430
```

Set `PUBLIC_BASE_URL=https://<machine>.<your-tailnet>.ts.net:8443` in `.env` and restart the container (`docker compose up -d`). The feed URLs appear at the bottom of the web page, each with a Copy button.

In Apple Podcasts, go to **Library → ⋯ → Follow a Show by URL**, paste the URL, then turn on **Automatically Download** in the show's settings. Apple Podcasts fetches private feeds from the phone itself, so it works over Tailscale.

Apps that fetch feeds from their own servers can't reach a tailnet address; Overcast and Pocket Casts are examples. If you need one of those, expose only `/feed/` and `/audio/` with Tailscale Funnel. The app refuses every other path for Funnel traffic.

## Deep dives: setting up the Mac worker

1. Render the worker prompt with your server's address:

   ```bash
   python3 scripts/render_worker_prompt.py --server http://<TAILSCALE_IP>:8430
   ```

   This writes `worker/deep-dive-task.md` (gitignored) and prints the remaining steps.
2. Save the server's `WORKER_TOKEN` into the header file the script names (`~/.config/morning-brief/worker-header`, mode 600).
3. Add the printed permission rules to `~/.claude/settings.json` under `permissions.allow`. They let the unattended hourly run search and read the web and talk only to your server, without stopping for approval.
4. In the Claude desktop app, create a scheduled task that runs hourly, with the contents of `worker/deep-dive-task.md`. Queue a topic in the web UI and press **Run now** once to check it end to end.

How it behaves:
- The worker runs whenever the Claude app is open. It keeps up to **3 unheard episodes** ready.
- **Mark heard** frees a slot, and so does waiting 7 days. Heard episodes are deleted after 30 days.
- An idle hourly check is a single request, so it costs next to nothing.

## Daily brief (optional)

Set `DAILY_BRIEF=1` and `ANTHROPIC_API_KEY` in `.env`. Pick your `LISTENER_LOCATION` and replace the `local` feeds in `feeds.yaml`, which default to Minneapolis. Episodes generate at `RUN_AT` (default 08:00) and retry at `RETRY_AT` if the first attempt fails.

Each run picks 6–8 stories. It uses full article text where the feed or `robots.txt`-permitted page provides it, and each story names its source out loud. The script is checked before speaking: 300–750 words, and every story must cite a real item.

## Customizing

| Setting | Where | Notes |
|---|---|---|
| Voice | `VOICE` in `.env` | Any Kokoro voice (`am_michael`, `af_heart`, `af_bella`, `bf_emma`, …) |
| Speaking pace | `SPEED` in `morning_brief/speech.py` | Default 1.1× |
| News sources | `feeds.yaml` | Grouped by segment: `headlines`, `tech`, `business`, `local` |
| Schedule and time zone | `RUN_AT`, `RETRY_AT`, `BRIEF_TZ` | Daily brief only |

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

- Kokoro's bundled espeak doesn't run on macOS. Set `FAKE_SPEECH=1` for silent placeholder audio on a Mac, and run the real-speech test inside the container:

  ```bash
  docker compose run --rm -v "$PWD":/work -w /work morning-brief sh -c "pip install -q pytest && python -m pytest -q -p no:cacheprovider -m container"
  ```

- `CLAUDE_OFFLINE=1` swaps the Anthropic API for a deterministic local stand-in. It lets you run the whole daily pipeline for free; it's a plumbing check, not a quality check.

## Credits

- **Speech:** [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0) via [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (MIT). The model is downloaded at runtime, not included here.
- **Web UI:** [htmx](https://htmx.org) (Zero-Clause BSD), vendored in `morning_brief/static/`. See `THIRD_PARTY_NOTICES.md`.
- **News content** belongs to its publishers. Episodes summarize and attribute stories for personal listening; they don't reproduce articles.
- Built with [Claude Code](https://claude.com/claude-code).

## License

MIT. See `LICENSE`.
