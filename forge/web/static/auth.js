// Password reveal toggle. The only JavaScript on the auth pages - every other
// piece of motion is CSS, so the form still works with scripting disabled.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-peek]");
  if (!button) return;

  const input = document.getElementById(button.dataset.peek);
  if (!input) return;

  const revealing = input.type === "password";
  input.type = revealing ? "text" : "password";
  button.classList.toggle("is-on", revealing);
  button.setAttribute("aria-label", revealing ? "Hide password" : "Show password");

  // Keep the caret where the user left it; switching type resets it to the start.
  const end = input.value.length;
  input.focus();
  input.setSelectionRange(end, end);
});
