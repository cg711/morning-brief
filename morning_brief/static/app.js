// Transcript seek buttons: jump the episode's player to a chapter start and play.
document.addEventListener("click", (event) => {
  const button = event.target.closest(".seek");
  if (!button) return;
  const audio = button.closest("article")?.querySelector("audio");
  if (!audio) return;
  const seconds = Number(button.dataset.seek);
  audio.play().catch(() => {}); // synchronous, inside the gesture, so iOS Safari allows it; this also starts the load
  if (audio.readyState >= 1) {
    audio.currentTime = seconds;
  } else {
    audio.addEventListener("loadedmetadata", () => { audio.currentTime = seconds; }, { once: true });
  }
});

// Deep-dives section swaps (Go deeper, polling) replace the whole section's outerHTML, which would
// otherwise stop playback and close any open <details>. Remember what was open and reopen it after.
let openDetailsIds = [];

document.addEventListener("htmx:beforeSwap", (event) => {
  openDetailsIds = [...event.detail.target.querySelectorAll("details[open][id]")].map((d) => d.id);
});

document.addEventListener("htmx:afterSettle", (event) => {
  for (const id of openDetailsIds) {
    const details = document.getElementById(id);
    if (details) details.open = true;
  }
});

// Copy buttons (data-copy="<input id>"). navigator.clipboard needs a secure context, which the plain-http
// tailnet UI isn't, so fall back to selecting the text and execCommand("copy").
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const input = document.getElementById(button.dataset.copy);
  if (!input) return;
  const done = () => { button.textContent = "Copied"; };
  const fallback = () => {
    input.focus();
    input.select();
    input.setSelectionRange(0, input.value.length);
    if (document.execCommand("copy")) done();
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(input.value).then(done, fallback);
  } else {
    fallback();
  }
});

// Share links select themselves when tapped, so they're easy to copy by hand too.
document.addEventListener("focusin", (event) => {
  if (event.target.matches && event.target.matches(".share-url")) event.target.select();
});
