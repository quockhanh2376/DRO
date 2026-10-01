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

let targetSearchTimer;
let targetSearchOriginalOrder = null;
const targetSearchCollator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });

function reorderTargetBlocks(input) {
  const table = input.closest(".panel")?.nextElementSibling?.querySelector("table")
    || document.querySelector(".table-wrap table");
  if (!table) return;
  const blocks = Array.from(table.querySelectorAll("tbody.target-block[data-hostname]"));
  if (!blocks.length) return;
  if (!targetSearchOriginalOrder) {
    targetSearchOriginalOrder = [...blocks].sort((a, b) =>
      targetSearchCollator.compare(a.dataset.hostname, b.dataset.hostname));
  }
  const query = input.value.trim().toLocaleLowerCase();
  const rank = (name) => !query ? 0 : name === query ? 0 : name.startsWith(query) ? 1 : name.includes(query) ? 2 : 3;
  const ordered = [...targetSearchOriginalOrder].sort((a, b) => {
    const left = a.dataset.hostname.trim().toLocaleLowerCase();
    const right = b.dataset.hostname.trim().toLocaleLowerCase();
    return rank(left) - rank(right) || targetSearchCollator.compare(a.dataset.hostname, b.dataset.hostname);
  });
  ordered.forEach((block, index) => {
    table.appendChild(block);
    const sequence = block.querySelector(".target-sequence");
    if (sequence) sequence.textContent = String(index + 1);
  });
  blocks.forEach((block) => {
    block.classList.remove("search-match", "search-best-match", "search-exact-match");
  });
  if (!query) return;
  const matches = ordered.filter((block) => block.dataset.hostname.trim().toLocaleLowerCase().includes(query));
  matches.forEach((block, index) => {
    block.classList.add("search-match");
    if (index === 0) block.classList.add("search-best-match");
    if (block.dataset.hostname.trim().toLocaleLowerCase() === query) {
      block.classList.add("search-exact-match");
    }
  });
}

// Delegation keeps search alive when HTMX updates descendants and avoids relying on
// this deferred asset being evaluated after the Targets page body has been parsed.
document.addEventListener("input", (event) => {
  if (!event.target.matches("#target-search-input")) return;
  window.clearTimeout(targetSearchTimer);
  targetSearchTimer = window.setTimeout(() => reorderTargetBlocks(event.target), 180);
});

document.addEventListener("click", (event) => {
  const clearButton = event.target.closest("#target-search-clear");
  if (!clearButton) return;
  const input = document.getElementById("target-search-input");
  if (!input) return;
  input.value = "";
  window.clearTimeout(targetSearchTimer);
  reorderTargetBlocks(input);
  input.focus();
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("#dns-check-form");
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  const result = document.getElementById("dns-check-result");
  if (!button || !result || button.disabled) return;
  const originalText = "Check DNS";
  button.disabled = true;
  button.textContent = "Checking...";
  try {
    const response = await fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" }
    });
    if (!response.ok) throw new Error("DNS check could not be completed. Please reload and try again.");
    result.innerHTML = await response.text();
  } catch (error) {
    result.textContent = error instanceof Error ? error.message : "DNS check could not be completed.";
    result.className = "dns-check-result dns-check-error";
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
});

document.addEventListener("htmx:afterSwap", () => {
  document.querySelectorAll(".ping-console-output").forEach((output) => {
    output.scrollTop = output.scrollHeight;
  });
});
