import { getScrollBehavior } from "./scroll-jump-core.js";

let cancelPendingReveal = () => {};

export function revealPlaceCard(target) {
  cancelPendingReveal();
  if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
  // Dismiss the keyboard before calculating the scroll destination.
  target.focus({ preventScroll: true });
  const mobile = window.matchMedia("(max-width: 979px)").matches;
  const scroll = () =>
    target.scrollIntoView({
      behavior: mobile
        ? "auto"
        : getScrollBehavior(window.matchMedia("(prefers-reduced-motion: reduce)").matches),
      block: mobile ? "start" : "center",
    });
  scroll();
  if (!mobile) return;

  // Android can resize/pan its visual viewport after focus leaves the chat input.
  // Re-align after that animation, without following a later manual interaction.
  const viewport = window.visualViewport;
  let settleTimer;
  const cleanup = () => {
    window.clearTimeout(settleTimer);
    window.clearTimeout(expiryTimer);
    window.removeEventListener("resize", onResize);
    viewport?.removeEventListener("resize", onResize);
    document.removeEventListener("pointerdown", cleanup, true);
    document.removeEventListener("keydown", cleanup, true);
  };
  const onResize = () => {
    window.clearTimeout(settleTimer);
    settleTimer = window.setTimeout(() => {
      if (document.activeElement === target && target.isConnected && !target.hidden) scroll();
    }, 120);
  };
  const expiryTimer = window.setTimeout(cleanup, 1200);
  window.addEventListener("resize", onResize);
  viewport?.addEventListener("resize", onResize);
  document.addEventListener("pointerdown", cleanup, true);
  document.addEventListener("keydown", cleanup, true);
  cancelPendingReveal = cleanup;
}
