const THIRD_PARTY_IMAGE_PATTERN =
  /(?:google|gstatic|googleapis|openstreetmap|wikimedia|cartocdn|mapbox|leaflet)/i;

function anchorAccessibleName(anchor: HTMLAnchorElement): string | null {
  const text = anchor.innerText?.trim();
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

function patchMapAccessibility(root: HTMLElement, label: string) {
  root.querySelectorAll<HTMLIFrameElement>("iframe:not([title])").forEach((iframe) => {
    iframe.title = label;
  });

  root.querySelectorAll<HTMLAnchorElement>("a[href]").forEach((anchor) => {
    if (anchor.innerText?.trim() || anchor.getAttribute("aria-label")?.trim()) return;

    const imgAlt = anchor.querySelector("img")?.getAttribute("alt")?.trim();
    if (imgAlt) {
      anchor.setAttribute("aria-label", imgAlt);
      return;
    }

    anchor.setAttribute("aria-label", anchorAccessibleName(anchor) ?? "Open map link");
  });

  root.querySelectorAll<HTMLImageElement>("img:not([data-image-component])").forEach((img) => {
    const src = img.getAttribute("src") || "";
    if (!src || src.startsWith("data:")) return;
    if (THIRD_PARTY_IMAGE_PATTERN.test(src)) {
      img.setAttribute("data-image-component", "");
    }
  });
}

export function installMapAccessibility(root: HTMLElement, label: string) {
  patchMapAccessibility(root, label);

  const observer = new MutationObserver(() => {
    patchMapAccessibility(root, label);
  });
  observer.observe(root, {
    attributes: true,
    attributeFilter: ["href", "src", "title", "aria-label"],
    childList: true,
    subtree: true,
  });

  return () => observer.disconnect();
}
