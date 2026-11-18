document.querySelectorAll(".bar[data-pct]").forEach((el) => {
  const pct = Math.max(0, Math.min(100, Number(el.dataset.pct) || 0));
  requestAnimationFrame(() => {
    const fill = el.querySelector("i");
    if (fill) fill.style.width = `${pct}%`;
  });
});

document.querySelectorAll("[data-copy]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const sel = btn.getAttribute("data-copy");
    const node = sel ? document.querySelector(sel) : null;
    const text = (node && node.textContent) || "";
    try {
      await navigator.clipboard.writeText(text.trim());
      const prev = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => {
        btn.textContent = prev && prev.startsWith("Copy") ? prev : "Copy";
      }, 1500);
    } catch (_) {
      /* ignore */
    }
  });
});

(() => {
  const form = document.getElementById("proposal-form");
  if (!form) return;
  const err = document.getElementById("asset-pick-error");
  const sync = (toggle) => {
    const ticker = toggle.getAttribute("data-ticker");
    const on = toggle.checked;
    form.querySelectorAll(`[data-ticker="${ticker}"]`).forEach((el) => {
      if (el === toggle) return;
      el.disabled = !on;
      if (!on && (el.classList.contains("asset-target") || el.classList.contains("asset-payout"))) {
        el.value = "";
      }
      if (el.classList.contains("asset-target")) {
        el.required = on;
      }
    });
  };
  form.querySelectorAll(".asset-toggle").forEach((toggle) => {
    toggle.addEventListener("change", () => sync(toggle));
    sync(toggle);
  });
  form.addEventListener("submit", (ev) => {
    const any = [...form.querySelectorAll(".asset-toggle")].some((t) => t.checked);
    if (!any) {
      ev.preventDefault();
      if (err) err.hidden = false;
    } else if (err) {
      err.hidden = true;
    }
  });
})();
