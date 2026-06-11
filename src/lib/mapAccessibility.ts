const MAP_ROOT_SELECTOR = "[data-guide-map], [data-home-guide-map]";
const GOOGLE_FRAME_PATTERN = /google|gstatic/i;

function mapRoots(root?: HTMLElement | null) {
  if (root?.matches(MAP_ROOT_SELECTOR)) {
    return [root];
  }

  const scopedRoots = root ? Array.from(root.querySelectorAll<HTMLElement>(MAP_ROOT_SELECTOR)) : [];
  if (scopedRoots.length > 0) {
    return scopedRoots;
  }

  return Array.from(document.querySelectorAll<HTMLElement>(MAP_ROOT_SELECTOR));
}

function anchorAccessibleName(anchor: HTMLAnchorElement): string | null {
  const text = anchor.textContent?.trim();
  if (text) return text;

  const ariaLabel = anchor.getAttribute("aria-label")?.trim();
  if (ariaLabel) return ariaLabel;

  const title = anchor.getAttribute("title")?.trim();
  if (title) return title;

  const imgAlt = anchor.querySelector("img")?.getAttribute("alt")?.trim();
  if (imgAlt) return imgAlt;

  try {
    const host = new URL(anchor.href, window.location.href).hostname.replace(/^www\./, "");
    return host ? `Open ${host}` : null;
  } catch {
    return null;
  }
}

function patchAnchors(container: ParentNode) {
  container.querySelectorAll<HTMLAnchorElement>("a[href]").forEach((anchor) => {
    if (
      anchor.textContent?.trim() ||
      anchor.getAttribute("aria-label")?.trim() ||
      anchor.getAttribute("aria-labelledby")?.trim()
    ) {
      return;
    }

    const imgAlt = anchor.querySelector("img")?.getAttribute("alt")?.trim();
    if (imgAlt) {
      anchor.setAttribute("aria-label", imgAlt);
      return;
    }

    anchor.setAttribute("aria-label", anchorAccessibleName(anchor) ?? "Open map link");
  });
}

function patchIframes(label: string, roots: HTMLElement[]) {
  roots.forEach((root) => {
    root.querySelectorAll<HTMLIFrameElement>("iframe:not([title])").forEach((iframe) => {
      iframe.title = label;
    });
  });

  document.querySelectorAll<HTMLIFrameElement>("iframe:not([title])").forEach((iframe) => {
    const src = iframe.getAttribute("src") || "";
    if (GOOGLE_FRAME_PATTERN.test(src) || iframe.closest(MAP_ROOT_SELECTOR)) {
      iframe.title = label;
    }
  });
}

function patchMapAccessibility(label: string, root?: HTMLElement | null) {
  const roots = mapRoots(root);

  roots.forEach((mapRoot) => {
    patchAnchors(mapRoot);
  });

  patchIframes(label, roots);
}

export function installMapAccessibility(root: HTMLElement, label: string) {
  const patch = () => patchMapAccessibility(label, root);
  let pendingPatchFrame: number | null = null;

  const schedulePatch = () => {
    if (pendingPatchFrame !== null) return;

    pendingPatchFrame = window.requestAnimationFrame(() => {
      pendingPatchFrame = null;
      patch();
    });
  };

  patch();

  const observers = mapRoots(root).map((mapRoot) => {
    const observer = new MutationObserver(schedulePatch);
    observer.observe(mapRoot, {
      attributes: true,
      attributeFilter: ["href", "src", "title", "aria-label", "aria-labelledby"],
      childList: true,
      subtree: true,
    });
    return observer;
  });

  const delayedPatchTimers = [250, 1000, 3000].map((delay) =>
    window.setTimeout(schedulePatch, delay),
  );

  return () => {
    observers.forEach((observer) => observer.disconnect());
    delayedPatchTimers.forEach((timer) => window.clearTimeout(timer));
    if (pendingPatchFrame !== null) {
      window.cancelAnimationFrame(pendingPatchFrame);
    }
  };
}
