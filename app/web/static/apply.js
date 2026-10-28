document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.matches(".apply-best-form")) return;
  if (form.dataset.applying === "true") {
    event.preventDefault();
    return;
  }

  const button = event.submitter || form.querySelector('button[type="submit"]');
  if (!button) return;
  form.dataset.applying = "true";
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.innerHTML = '<span class="apply-spinner" aria-hidden="true"></span> Applying...';
});
