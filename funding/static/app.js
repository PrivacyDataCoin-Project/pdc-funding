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
      btn.textContent = "Copied";
      setTimeout(() => {
        btn.textContent = "Copy";
      }, 1500);
    } catch (_) {
      /* ignore */
    }
  });
});
