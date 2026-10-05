(() => {
  let activeRequest = null;

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("#run-diagnostics");
    if (!button || activeRequest) return;

    const result = document.getElementById("diagnostics-result");
    if (!result) return;

    const originalLabel = button.textContent.trim();
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.innerHTML = '<span class="diagnostics-spinner" aria-hidden="true"></span>Running diagnostics...';

    activeRequest = fetch(button.dataset.url, {
      method: "POST",
      headers: {"X-CSRF-Token": button.dataset.csrf, "X-Requested-With": "fetch"},
      credentials: "same-origin",
    });

    try {
      const response = await activeRequest;
      if (!response.ok) throw new Error(`Diagnostics request failed (${response.status})`);
      result.innerHTML = await response.text();
    } catch (_error) {
      result.innerHTML = '<section class="diagnostics-panel diagnostics-request-error" role="alert">Diagnostics could not be completed. Please try again.</section>';
    } finally {
      activeRequest = null;
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = originalLabel;
    }
  });
})();
