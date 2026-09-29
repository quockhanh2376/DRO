document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const value = button.dataset.copy;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
    } else {
      const input = document.createElement("textarea");
      input.value = value;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.append(input);
      input.select();
      document.execCommand("copy");
      input.remove();
    }
    const originalTitle = button.dataset.copyTitle || "Copy hostname";
    button.title = "Copied";
    window.setTimeout(() => { button.title = originalTitle; }, 1200);
  } catch {
    button.title = "Copy failed";
  }
});

const searchForm = document.getElementById("target-search-form");
if (searchForm) {
  const searchInput = document.getElementById("target-search-input");
  const clearButton = document.getElementById("target-search-clear");
  const message = document.getElementById("target-search-message");
  const targets = Array.from(document.querySelectorAll(".target-primary-row"));
  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = searchInput.value.trim().toLocaleLowerCase();
    message.textContent = "";
    if (!query) {
      searchInput.focus();
      return;
    }
    const exact = targets.filter((row) => row.dataset.hostname.trim().toLocaleLowerCase() === query);
    const matches = exact.length ? exact : targets.filter((row) => row.dataset.hostname.toLocaleLowerCase().includes(query));
    if (!matches.length) {
      message.textContent = "No matching domain found.";
      return;
    }
    const row = matches[0];
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.remove("target-search-highlight");
    void row.offsetWidth;
    row.classList.add("target-search-highlight");
    window.setTimeout(() => row.classList.remove("target-search-highlight"), 2200);
    if (matches.length > 1) message.textContent = `${matches.length} matches found; showing first.`;
  });
  clearButton.addEventListener("click", () => {
    searchInput.value = "";
    message.textContent = "";
    searchInput.focus();
  });
}

document.addEventListener("htmx:afterSwap", () => {
  document.querySelectorAll(".ping-console-output").forEach((output) => {
    output.scrollTop = output.scrollHeight;
  });
});
