/* Floor-plan zoom. "Fit" shows the whole plan in the box (width and height), centred;
   + / − zoom from there. Plans open fitted. */
window.OpnPlanZoom = function (wrap, inner, onchange) {
  const img = inner.querySelector('img');
  let level = 1; // 1 = fit
  function fitWidth() {
    const ratio = img.naturalWidth ? img.naturalHeight / img.naturalWidth : 0.75;
    const maxH = parseFloat(getComputedStyle(wrap).maxHeight) || innerHeight * 0.74;
    const top = wrap.getBoundingClientRect().top + scrollY;  // room left on screen below the box's top
    const availW = wrap.clientWidth, availH = Math.min(maxH, Math.max(280, innerHeight - top - 20)) - 2;
    return Math.max(120, Math.min(availW, availH / ratio));
  }
  function apply() {
    inner.style.width = Math.round(fitWidth() * level) + 'px';
    if (onchange) onchange();
  }
  document.querySelectorAll('[data-zoom]').forEach(b => b.addEventListener('click', () => {
    const z = +b.dataset.zoom;
    level = z === 0 ? 1 : Math.min(6, Math.max(1, z > 0 ? level * 1.5 : level / 1.5));
    if (level < 1.01) level = 1;
    apply();
  }));
  if (img.complete) apply(); else img.addEventListener('load', apply);
  addEventListener('resize', apply);
  return { apply };
};
