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
let targetSearchPreviousFirst = null;
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
  const first = query ? ordered.find((block) => block.dataset.hostname.trim().toLocaleLowerCase().includes(query)) : null;
  if (targetSearchPreviousFirst && targetSearchPreviousFirst !== first) {
    targetSearchPreviousFirst.classList.remove("target-search-highlight");
  }
  if (first && first !== targetSearchPreviousFirst) {
    first.classList.remove("target-search-highlight");
    void first.offsetWidth;
    first.classList.add("target-search-highlight");
    window.setTimeout(() => first.classList.remove("target-search-highlight"), 1500);
  }
  targetSearchPreviousFirst = first;
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

document.addEventListener("htmx:afterSwap", () => {
  document.querySelectorAll(".ping-console-output").forEach((output) => {
    output.scrollTop = output.scrollHeight;
  });
});
