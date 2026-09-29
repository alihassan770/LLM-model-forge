// The expand button on the in-page 3D viewer.
//
// model-viewer itself needs no wiring: the custom element handles loading,
// framing, orbiting and zooming once its script is on the page. This is only
// the full-screen control, which the browser provides and the element does
// not.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-expand]");
  if (!button) return;

  const box = document.getElementById(button.dataset.expand);
  if (!box) return;

  // requestFullscreen rejects when the document is not allowed it (an iframe
  // without the permission, or Safari before a gesture). Nothing useful can
  // be done about that, so the failure is swallowed rather than logged as an
  // unhandled rejection in the console.
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  } else if (box.requestFullscreen) {
    box.requestFullscreen().catch(() => {});
  }
});
