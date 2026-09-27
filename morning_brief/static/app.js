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
