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
  const table = document.querySelector(".table-wrap table");
  const tbodyList = Array.from(table.querySelectorAll("tbody.target-block"));
  const collator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });
  const originalOrder = [...tbodyList].sort((a, b) => collator.compare(a.dataset.hostname, b.dataset.hostname));
  let timer;
  let previousFirst = null;
  const updateOrder = () => {
    const query = searchInput.value.trim().toLocaleLowerCase();
    const ranked = [...originalOrder].sort((a, b) => {
      const left = a.dataset.hostname.trim().toLocaleLowerCase();
      const right = b.dataset.hostname.trim().toLocaleLowerCase();
      const rank = (name) => !query ? 0 : name === query ? 0 : name.startsWith(query) ? 1 : name.includes(query) ? 2 : 3;
      return rank(left) - rank(right) || collator.compare(a.dataset.hostname, b.dataset.hostname);
    });
    ranked.forEach((block, index) => {
      table.append(block);
      block.querySelector(".target-sequence").textContent = String(index + 1);
    });
    const first = query ? ranked.find((block) => block.dataset.hostname.toLocaleLowerCase().includes(query)) : null;
    if (previousFirst && previousFirst !== first) previousFirst.classList.remove("target-search-highlight");
    if (first && first !== previousFirst) {
      first.classList.remove("target-search-highlight");
      void first.offsetWidth;
      first.classList.add("target-search-highlight");
      window.setTimeout(() => first.classList.remove("target-search-highlight"), 1500);
    }
    previousFirst = first;
  };
  searchInput.addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(updateOrder, 180);
  });
  clearButton.addEventListener("click", () => {
    searchInput.value = "";
    window.clearTimeout(timer);
    updateOrder();
    searchInput.focus();
  });
}

document.addEventListener("htmx:afterSwap", () => {
  document.querySelectorAll(".ping-console-output").forEach((output) => {
    output.scrollTop = output.scrollHeight;
  });
});
