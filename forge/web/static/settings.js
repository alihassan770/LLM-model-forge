// Settings page: show the model list belonging to whichever provider is
// selected. The server re-validates the combination on save, so this is
// convenience rather than enforcement.

function showProvider(providerId) {
  document.querySelectorAll("[data-models]").forEach((block) => {
    const mine = block.dataset.models === providerId;
    block.hidden = !mine;
    // A disabled control is not submitted. Without this every hidden provider
    // would also post a "model" value and the server would take whichever
    // happened to arrive first. The typed-id field needs the same treatment
    // for a sharper reason: it overrides the dropdown, so text left behind in
    // a hidden OpenRouter block would otherwise decide the model for whatever
    // provider was actually chosen.
    block
      .querySelectorAll("select, input[name='model_custom']")
      .forEach((control) => (control.disabled = !mine));
  });

  document.querySelectorAll(".provider-opt").forEach((opt) => {
    opt.classList.toggle(
      "is-on",
      opt.querySelector("input").value === providerId
    );
  });
}

document.addEventListener("change", (event) => {
  if (event.target.name === "provider") {
    showProvider(event.target.value);
  }
});

// OpenRouter carries a few hundred models. A dropdown that long is a haystack,
// so the filter box narrows it as you type.
//
// Progressive enhancement, like everything else on this page: with JavaScript
// off the full grouped list is still there and still selectable, and the
// server does not care how the value was chosen. This only makes it findable.
(function orFilter() {
  const box = document.getElementById("or-filter");
  const select = document.getElementById("or-model");
  const count = document.getElementById("or-count");
  if (!box || !select) return;

  const total = select.querySelectorAll("option").length;
  const summary = count ? count.textContent.trim() : "";

  box.addEventListener("input", () => {
    const needle = box.value.trim().toLowerCase();
    let shown = 0;

    select.querySelectorAll("optgroup").forEach((group) => {
      let anyVisible = false;
      group.querySelectorAll("option").forEach((option) => {
        // Match the id and the display label together, so "claude" finds
        // "anthropic/claude-sonnet-4.5" and "Anthropic: Claude Sonnet 4.5".
        const hay = (option.value + " " + option.textContent).toLowerCase();
        const hit = !needle || hay.includes(needle);
        option.hidden = !hit;
        if (hit) {
          anyVisible = true;
          shown += 1;
        }
      });
      // A group whose every option is hidden still renders its label in some
      // browsers, leaving a list of headings over nothing.
      group.hidden = !anyVisible;
    });

    if (!count) return;
    count.textContent = needle
      ? `${shown} of ${total} models match “${box.value.trim()}”.`
      : summary;
  });
})();

// Role cards on the members page use the same selected-state pattern.
document.addEventListener("change", (event) => {
  if (event.target.name !== "role" || event.target.type !== "radio") return;
  document.querySelectorAll(".role-card").forEach((card) => {
    card.classList.toggle("is-on", card.contains(event.target));
  });
});

// Thinking level: move the selected pill and swap the description under it.
//
// Progressive enhancement. The radios post their value with or without this,
// and the server renders the correct pill and description on load, so with
// JavaScript off the control still works and still reads correctly. This only
// saves a round trip while choosing.
document.addEventListener("change", (event) => {
  if (event.target.name !== "effort" || event.target.type !== "radio") return;

  document.querySelectorAll(".seg-opt").forEach((opt) => {
    opt.classList.toggle("is-on", opt.contains(event.target));
  });

  const note = document.getElementById("effort-note");
  if (note) note.textContent = event.target.dataset.note || "";
});
