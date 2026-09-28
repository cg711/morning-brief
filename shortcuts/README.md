# "Morning Brief" share-sheet Shortcut

Share a link or some text from any app on your iPhone, and it joins your deep-dive queue. The phone needs Tailscale on, because the endpoint is only reachable on your tailnet.

## 1. Turn on the inbox

Put a long random `INBOX_TOKEN` in the server's `.env` and restart:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## 2a. Install the generated Shortcut (on a Mac)

```bash
python3 scripts/build_shortcut.py --server http://<your-tailnet-ip>:8430 --out "Morning Brief.shortcut"
shortcuts sign --mode anyone --input "Morning Brief.shortcut" --output "Morning Brief (signed).shortcut"
```

AirDrop the signed file to your phone and tap **Add Shortcut**. When asked, paste your `INBOX_TOKEN`.

## 2b. Or build it by hand (Shortcuts app on the phone)

1. Create a new Shortcut named **Morning Brief**. In its settings (ⓘ), turn on **Show in Share Sheet** and set it to receive **URLs** and **Text**. Set "If there's no input" to **Ask For** Text.
2. **Text** action: paste your `INBOX_TOKEN`. Then **Set Variable** `Token` to that Text.
3. **Text** action containing **Shortcut Input**.
4. **Ask for Input** (Text), with the prompt `Anything to focus on? (optional)`.
5. **Choose from Menu** with the prompt `Where in the queue?` and two items:
   - Top of queue: a **Text** action `top`, then **Set Variable** `Position`.
   - End of queue: a **Text** action `end`, then **Set Variable** `Position`.
6. **Get Contents of URL**: `http://<your-tailnet-ip>:8430/api/inbox`, with:
   - Method **POST**;
   - Headers: `Authorization` = `Bearer ` followed by the `Token` variable;
   - Request Body **JSON**:
     - `input` = the Text from step 3;
     - `note` = **Provided Input**;
     - `position` = the `Position` variable.
7. **Get Dictionary Value** for the key `message` in **Contents of URL**.
8. **Show Notification**: **Dictionary Value**.

## What you'll see

- `Queued, 2nd in line: From link: example.com/story`
- `Queued at the top: How the Fed began`
- `Already in your queue: …` (sharing the same link again won't queue it twice while it's waiting or ready)
- `Wrong token.` or `The inbox is off: set INBOX_TOKEN.`

If Tailscale is off, Shortcuts shows its own "could not connect" error.
