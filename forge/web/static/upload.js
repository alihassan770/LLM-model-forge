// Drag-and-drop and selection feedback for the upload form.
// Progressive enhancement only: the form is an ordinary multipart POST and
// works exactly the same with JavaScript disabled.

const dropzone = document.getElementById("dropzone");
const input = document.getElementById("file-input");
const picked = document.getElementById("dz-picked");
const form = document.getElementById("upload-form");
const button = document.getElementById("upload-btn");
const hint = document.getElementById("upload-hint");

if (dropzone && input) {
  const describe = (files) => {
    if (!files || !files.length) {
      picked.hidden = true;
      return;
    }
    const total = [...files].reduce((n, f) => n + f.size, 0);
    const mb = (total / 1024 / 1024).toFixed(1);
    const names = [...files].map((f) => f.name);
    picked.textContent =
      files.length === 1
        ? `${names[0]} · ${mb} MB`
        : `${files.length} files · ${mb} MB`;
    picked.hidden = false;
  };

  input.addEventListener("change", () => describe(input.files));

  ["dragenter", "dragover"].forEach((event) =>
    dropzone.addEventListener(event, (e) => {
      e.preventDefault();
      dropzone.classList.add("is-over");
    })
  );

  ["dragleave", "drop"].forEach((event) =>
    dropzone.addEventListener(event, (e) => {
      e.preventDefault();
      dropzone.classList.remove("is-over");
    })
  );

  dropzone.addEventListener("drop", (e) => {
    if (!e.dataTransfer?.files?.length) return;
    // Assigning to input.files is what makes the dropped files part of the
    // normal form submission, so the server path stays identical either way.
    input.files = e.dataTransfer.files;
    describe(input.files);
  });
}

if (form && button) {
  form.addEventListener("submit", () => {
    if (!input?.files?.length) return;
    // Large uploads take a moment and the page gives no other sign of life;
    // without this people click twice and send the whole thing again.
    button.disabled = true;
    button.textContent = "Uploading…";
    if (hint) hint.textContent = "Do not close this tab.";
  });
}

// Flag an answer that falls outside the expected range. A warning, never a
// block - an unusual object is still a real object, and the person with the
// tape measure is better informed than the model that guessed the range.
document.addEventListener("input", (event) => {
  const field = event.target;
  if (!field.classList?.contains("q-value")) return;

  const min = parseFloat(field.dataset.min);
  const max = parseFloat(field.dataset.max);
  const value = parseFloat(field.value);
  const out = !isNaN(value) && !isNaN(min) && !isNaN(max) &&
              (value < min || value > max);

  field.classList.toggle("out-of-range", out);

  const form = field.closest(".q-form");
  let hint = form.querySelector(".q-range-hint");
  if (out && !hint) {
    hint = document.createElement("span");
    hint.className = "q-range-hint";
    form.appendChild(hint);
  }
  if (hint) {
    hint.textContent = out
      ? `That is outside the expected ${min}-${max}. Saved anyway if correct.`
      : "";
  }
});
