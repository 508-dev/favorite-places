import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const readSource = (path) => readFileSync(path, "utf8");
const normalizeWhitespace = (value) => value.replace(/\s+/g, " ").trim();
const expectCssToContain = (css, snippet) => {
  expect(normalizeWhitespace(css)).toContain(normalizeWhitespace(snippet));
};
const expectCssNotToContain = (css, snippet) => {
  expect(normalizeWhitespace(css)).not.toContain(normalizeWhitespace(snippet));
};
const cssBlocks = (css, selector) => {
  return [...css.matchAll(/([^{}]+){([^}]*)}/g)]
    .filter((match) =>
      match[1]
        .split(",")
        .map((part) => part.trim())
        .includes(selector),
    )
    .map((match) => match[2]);
};

describe("guide map interactions", () => {
  it("keeps guide search controls in normal document flow while the map remains sticky", () => {
    const css = readSource("src/styles/global.css");

    expect(cssBlocks(css, ".controls-panel").join("\n")).not.toContain("position: sticky");
    expect(cssBlocks(css, ".country-browser").join("\n")).toContain("position: sticky");
    expectCssToContain(css, ".browse-layout > .map-panel {\n    order: 0;\n    position: sticky;");
  });

  it("overlays compact mobile map controls without reserving toolbar height", () => {
    const guideMap = readSource("src/components/GuideMap.astro");
    const css = readSource("src/styles/global.css");

    expect(guideMap).toContain('<div class="map-toolbar">');
    expect(guideMap).toContain('<span class="map-toggle-label">Hide map</span>');
    expectCssToContain(css, ".map-toolbar");
    expect(cssBlocks(css, ".map-panel").join("\n")).toContain("position: relative");
    expect(cssBlocks(css, ".map-actions").join("\n")).toContain("margin-left: 0");
    expectCssToContain(css, "@media (max-width: 979px)");
    expectCssToContain(
      css,
      ".map-panel {\n    --mobile-guide-map-height: clamp(22rem, 64svh, 34rem);",
    );
    expectCssToContain(
      css,
      ".map-panel {\n    --mobile-guide-map-height: clamp(22rem, 64svh, 34rem);\n\n    gap: 0;\n    padding: 0;",
    );
    expectCssToContain(css, ".home-map-stage {\n  position: relative;");
    expectCssToContain(css, ".home-map-stage .home-map-actions {\n  position: absolute;");
    expectCssToContain(css, ".map-inline-toggle {\n  min-height: 2.05rem;");
    expectCssToContain(css, "border-radius: 999px;");
    expectCssToContain(css, ".home-map-panel {\n    gap: 0.85rem;\n    padding: 0.85rem;");
    expectCssToContain(css, ".map-toolbar {\n    display: contents;");
    expectCssToContain(css, ".map-toggle {\n    position: absolute;\n    top: 0.85rem;");
    expectCssToContain(
      css,
      ".map-actions {\n    position: absolute;\n    top: 0.85rem;\n    right: 0.85rem;",
    );
    expectCssToContain(css, ".map-feedback-slot {\n    position: absolute;\n    top: 3.35rem;");
    expectCssToContain(css, ".guide-map {\n  width: 100%;\n  min-height: 16rem;");
    expectCssToContain(css, ".map-panel {\n    order: 0;\n    position: sticky;\n    top: 0.5rem;");
  });

  it("keeps place map popups compact, closeable, and informative", () => {
    const guideMap = readSource("src/components/GuideMap.astro");
    const css = readSource("src/styles/global.css");

    expect(guideMap).toContain("rating: place.rating");
    expect(guideMap).toContain("userRatingCount: place.user_rating_count");
    expect(guideMap).toContain("const popupCompactReviewFormatter = new Intl.NumberFormat");
    expect(guideMap).toContain('class="guide-map-popup-place-card"');
    expect(guideMap).toContain('class="guide-map-popup-rating"');
    expect(guideMap).toContain('class="guide-map-popup-map-action"');
    expect(guideMap).toContain('class="guide-map-popup-details-action"');
    expect(guideMap).toContain("data-guide-map-popup-close");
    expect(guideMap).toContain("data-guide-map-popup-details");
    expect(guideMap).not.toContain(">Open in Google Maps<");
    expect(guideMap).not.toContain("<span>Maps</span>");
    expect(guideMap).toContain("closeButton: false");
    expect(guideMap).toContain("headerDisabled: true");
    expect(guideMap).toContain(
      "const bindLeafletPopupActions = (marker: L.Marker, place: MapPlace) => {",
    );
    expect(guideMap).toContain('marker.on("popupopen", (event: L.PopupEvent) => {');
    expect(guideMap).toContain(
      'const closeButton = popupElement?.querySelector<HTMLButtonElement>("[data-guide-map-popup-close]");',
    );
    expect(guideMap).toContain(
      'const detailsButton = popupElement?.querySelector<HTMLButtonElement>("[data-guide-map-popup-details]");',
    );
    expect(guideMap).toContain("closeButton.onclick = () => {");
    expect(guideMap).toContain("detailsButton.onclick = (clickEvent) => {");
    expect(guideMap).toContain("onShowPlaceDetails(place.id);");
    expect(guideMap).toContain(
      "const handlePopupDetailsSelect = (placeId: string) => selectPlace(placeId, { focusCard: true });",
    );
    expect(guideMap).toContain('marker.on("click", () => onSelectPlace(place.id));');
    expect(guideMap).toContain('target?.closest("[data-guide-map-popup-close]")');
    expect(guideMap).toContain('target?.closest<HTMLElement>("[data-guide-map-popup-details]")');
    expectCssToContain(css, ".guide-map-popup-close");
    expectCssToContain(
      css,
      ".guide-map .gm-style-iw-c:has(.guide-map-popup-close) .gm-style-iw-chr",
    );
    expectCssToContain(css, ".guide-map-popup-place-card");
    expectCssToContain(css, ".guide-map-popup-map-action");
    expectCssToContain(css, ".guide-map-popup-details-action");
    expectCssToContain(css, "@media (min-width: 1280px)");
    expectCssToContain(css, "@media (max-width: 1279px)");
    expectCssToContain(css, "width: min(74vw, 15.5rem);");
    expectCssToContain(css, "max-height: min(54svh, 16rem);");
    expectCssToContain(css, "overflow-y: auto;");
    expectCssToContain(css, "height: clamp(4.75rem, 16svh, 6rem);");
    expectCssToContain(css, ".guide-map-popup-copy");
    expectCssToContain(css, ".guide-map-popup-photo img");
    expect(cssBlocks(css, ".guide-map-popup-photo img").join("\n")).toContain(
      "object-position: center top",
    );
    expectCssToContain(css, ".guide-map-popup-content .guide-map-popup-rating");
    expectCssToContain(css, "font-size: 0.68rem;");
    expectCssToContain(css, "white-space: nowrap;");
  });

  it("scrolls the full place card into view when a map marker is selected", () => {
    const guideMap = readSource("src/components/GuideMap.astro");

    expect(guideMap).toContain(
      "const shouldAutoFocusCardFromMarker = () => window.matchMedia(SIDE_BY_SIDE_MAP_MEDIA_QUERY)",
    );
    expect(guideMap).toContain(
      "selectPlace(placeId, { focusCard: shouldAutoFocusCardFromMarker() })",
    );
    expect(guideMap).toContain("const focusPlaceCard = (placeId: string) => {");
    expect(guideMap).not.toContain("card.focus({ preventScroll: true });");
    expect(guideMap).toContain(
      'card.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });',
    );
    expect(guideMap).toContain("selectPlace(placeId, { expandCollapsed: true });");
  });

  it("tightens the mobile guide chrome to save vertical space", () => {
    const css = readSource("src/styles/global.css");

    expectCssToContain(css, ".hero p,\n.lede {\n  margin: 0;");
    expectCssToContain(
      css,
      "@media (max-width: 719px) {\n  .page-shell,\n  .ui-page-shell {\n    width: min(calc(100% - 0.5rem), var(--page-width));",
    );
    expectCssToContain(css, ".site-header,\n  .ui-site-header {\n    padding: 0.35rem 0 0.2rem;");
    expectCssToContain(
      css,
      ".site-title img,\n  .ui-site-title img {\n    width: min(84px, 22vw);",
    );
    expectCssToContain(css, ".site-nav,\n  .ui-site-nav {\n    gap: 0.6rem;");
    expectCssToContain(css, ".hero {\n    padding-bottom: 1.4rem;");
    expectCssToContain(css, ".hero-grid,\n  .section-stack {\n    gap: 0.55rem;");
    expectCssToContain(css, ".stat-pill,\n  .action-pill,\n  .tag-pill {\n    min-height: 2rem;");
    expectCssToContain(css, ".section {\n    margin-top: 1.5rem;");
    expectCssToContain(css, ".social-card {\n    padding: 0.6rem;");
    expectCssToContain(css, ".social-card-copy {\n    font-size: 0.88rem;");
    expectCssToContain(css, ".social-card-link {\n    padding: 0.42rem 0.45rem;");
    expectCssToContain(css, ".social-card-icon {\n    width: 1.5rem;");
  });

  it("keeps the guide hero aside compact at intermediate desktop widths", () => {
    const css = readSource("src/styles/global.css");

    expectCssToContain(css, ".hero-grid > .section-stack:last-child:has(.social-card)");
    expectCssToContain(css, "@media (min-width: 720px) and (max-width: 1279px)");
    expectCssToContain(css, "grid-template-columns: minmax(0, 1fr);");
    expectCssToContain(css, "width: min(100%, 34rem);");
    expectCssToContain(css, "grid-template-columns: repeat(3, minmax(0, 1fr));");
    expectCssToContain(css, "margin-top: 0;");
    expectCssToContain(css, "font-size: 0.95rem;");
    expect(css).not.toContain("@media (min-width: 1280px) and (max-width: 1439px)");
    expectCssToContain(
      css,
      ".hero-grid > .section-stack:last-child .social-card-grid .social-card-link-copy",
    );
  });

  it("renders the Google Maps action as a compact icon button beside the place name", () => {
    const placeCard = readSource("src/components/PlaceCard.astro");
    const css = readSource("src/styles/global.css");

    expect(placeCard).toContain(
      'import { buildMapMarkerSvg, getMapMarkerColors } from "../lib/mapMarkerIcons";',
    );
    expect(placeCard).toContain(
      "getMapMarkerColors(place.marker_icon, { topPick: featured || place.top_pick })",
    );
    expect(placeCard).toContain('class="place-card-name-row"');
    expect(placeCard).toContain('class="place-card-marker"');
    expect(placeCard).toContain('class="place-card-map-link"');
    expect(placeCard).toContain("aria-label={`Open ${place.name} in Google Maps`}");
    expect(placeCard).toContain('<img src="/icons/google-maps.svg" alt="" width="18" height="26"');
    expect(placeCard).toContain('class="place-card-map-link-label"');
    expect(placeCard).toContain("{siteConfig.placeCard.mapsLabel}</span>");
    expect(placeCard).not.toContain("badge badge--featured");
    expect(placeCard).not.toContain(">Open in Google Maps<");
    expect(placeCard).toContain("set:html={markerSvg}");
    expectCssToContain(css, ".place-card-map-link");
    expectCssToContain(css, ".place-card-map-link img");
    expectCssToContain(css, ".place-card-map-link-label");
    expectCssToContain(css, ".place-card-marker");
    expectCssToContain(css, ".place-card-name-row");
    expectCssToContain(css, "width: 1.8rem;");
    expectCssToContain(css, "display: inline-flex;");
    expectCssToContain(css, "width: 2.45rem;");
    expectCssToContain(css, "justify-content: center;");
    expectCssToContain(css, "padding: 0;");
    expectCssToContain(css, ".place-card-map-link-label {\n    display: none;");
    expectCssToContain(css, "background: color-mix(in srgb, var(--surface) 92%, white);");
    expectCssToContain(css, "color: #18457a;");
    expectCssToContain(css, "border-radius: 999px;");
  });

  it("renders a single accented recommendation block for place copy", () => {
    const placeCard = readSource("src/components/PlaceCard.astro");
    const css = readSource("src/styles/global.css");

    expect(placeCard).toContain(
      "const whyRecommendedHtml = renderRichText(place.why_recommended ?? place.note);",
    );
    expect(placeCard).toContain('class="rich-copy ui-copy place-card-description"');
    expect(placeCard).not.toContain("place-card-copy-stack");
    expect(placeCard).not.toContain("place-card-note");
    expectCssToContain(css, ".place-card-description {\n    padding-left: 0.72rem;");
    expectCssToContain(
      css,
      "border-left: 2px solid color-mix(in srgb, var(--accent) 28%, var(--line));",
    );
  });

  it("reflows place stats by card width instead of branching on photo presence", () => {
    const placeCard = readSource("src/components/PlaceCard.astro");
    const css = readSource("src/styles/global.css");

    expect(placeCard).toContain("const placeRatingMetaLabel = [");
    expect(placeCard).toContain("siteConfig.placeCard.showRating && ratingValue !== null");
    expect(placeCard).toContain("siteConfig.placeCard.showReviewCount ? compactReviewLabel : null");
    expect(placeCard).toContain('class="stat-pill ui-pill place-card-rating-pill"');
    expect(placeCard).toContain('class="place-card-meta-row"');
    expect(placeCard).toContain(
      'class="stats-row ui-meta-row place-card-stats place-card-meta-stats"',
    );
    expect(placeCard).not.toContain("{hasPhoto && hasStats && (");
    expect(placeCard).not.toContain("{!hasPhoto && hasStats && (");
    expectCssToContain(css, ".place-card-meta-row");
    expectCssToContain(css, ".place-card-meta-stats");
    expectCssToContain(css, ".place-card-rating-pill");
    expectCssToContain(css, "grid-template-columns: auto minmax(0, 1fr);");
    expectCssToContain(css, ".place-card-meta-row .meta-copy");
    expectCssToContain(css, "-webkit-line-clamp: 2;");
    expectCssToContain(css, "grid-column: 1 / -1;");
  });

  it("lets the collapsed map panel release width back to the places list", () => {
    const guidePage = readSource("src/pages/guides/[slug].astro");
    const homeMap = readSource("src/components/HomeGuideMap.astro");
    const guideMap = readSource("src/components/GuideMap.astro");
    const css = readSource("src/styles/global.css");

    expect(guidePage).toContain('class="browse-layout" data-browse-layout');
    expect(homeMap).toContain('class="map-panel home-map-panel"');
    expect(guidePage).toContain('class="browse-main" data-browse-main');
    expect(guidePage.indexOf('<div class="browse-main" data-browse-main>')).toBeLessThan(
      guidePage.indexOf("<GuideMap places={visiblePlaces} />"),
    );
    expect(guideMap).toContain('const DESKTOP_MAP_MEDIA_QUERY = "(min-width: 980px)"');
    expect(guideMap).toContain("const syncPanelOrder = (panel: HTMLElement) => {");
    expect(guideMap).toContain(
      'const browseMain = layout?.querySelector<HTMLElement>("[data-browse-main]")',
    );
    expect(guideMap).toContain("browseMain.after(panel);");
    expect(guideMap).toContain("layout.prepend(panel);");
    expect(guideMap).toContain('panel.closest<HTMLElement>("[data-browse-layout]")');
    expect(guideMap).toContain("layout.dataset.mapCollapsed");
    expectCssToContain(css, '.browse-layout[data-map-collapsed="true"]');
    expectCssToContain(css, "grid-template-columns: minmax(0, 1fr);");
    expectCssToContain(css, ".browse-layout > .map-panel {\n    order: 0;\n    position: sticky;");
    expectCssToContain(css, '.browse-layout > .map-panel[data-collapsed="true"]');
    expectCssToContain(css, "position: fixed;");
    expectCssToContain(css, "right: 0;");
    expectCssToContain(css, ".home-map-panel {\n  position: relative;\n  top: auto;");
    expectCssToContain(
      css,
      '.browse-layout[data-map-collapsed="true"] .card-grid[data-kind="places"]',
    );
    expectCssToContain(css, "grid-template-columns: repeat(2, minmax(0, 1fr));");
    expectCssNotToContain(css, "grid-template-columns: repeat(auto-fit, minmax(23rem, 28rem))");
    expect(cssBlocks(css, ".map-panel").join("\n")).toContain("order: 0;");
    expectCssToContain(css, '.map-panel[data-collapsed="true"] .map-toggle');
    expectCssToContain(css, "border-color: transparent");
    expectCssToContain(css, "background: transparent");
    expectCssNotToContain(css, "inset: 0;\n  width: 100%;\n  height: 100%;");
  });

  it("keeps map-frame filtering resettable from both the map and the list", () => {
    const guidePage = readSource("src/pages/guides/[slug].astro");
    const guideMap = readSource("src/components/GuideMap.astro");
    const filters = readSource("public/scripts/guide-filters.js");

    expect(guidePage).toContain("data-area-filter-status");
    expect(guidePage).toContain("data-map-filter-status");
    expect(guidePage).toContain("data-tag-count-text");
    expect(guidePage).toContain("data-map-filter-reset");
    expect(guideMap).toContain("data-map-frame-filter");
    expect(guideMap).toContain("data-map-full-area");
    expect(guideMap).toContain(">Reset Map</button>");
    expect(guideMap).toContain("const capFittedZoomAndCapture = () => {");
    expect(guideMap).toContain("if (zoom !== undefined && zoom > focusMaxResetZoom) {");
    expect(guideMap).toContain("map.setZoom(focusMaxResetZoom);");
    expect(guideMap).toContain("capFittedZoomAndCapture();");
    expect(guideMap).toContain('new CustomEvent("guide:map-frame-reset-request"');
    expect(guideMap).toContain('new CustomEvent("guide:map-frame-filter"');
    expect(filters).toContain("countMatchingCards");
    expect(filters).toContain("buildAreaFilterStatusMessage");
    expect(filters).toContain('root.addEventListener("guide:map-frame-filter"');
    expect(filters).toContain('new CustomEvent("guide:map-frame-reset"');
  });

  it("guards the current-location map control with stored guide proximity bounds", () => {
    const guideMap = readSource("src/components/GuideMap.astro");
    const guidePage = readSource("src/pages/guides/[slug].astro");
    const filters = readSource("public/scripts/guide-filters.js");
    const css = readSource("src/styles/global.css");

    expect(guideMap).toContain('class="map-icon-button"');
    expect(guideMap).toContain('class="map-feedback-slot"');
    expect(guideMap).toContain('class="map-stage"');
    expect(guideMap).toContain('data-location-state="idle"');
    expect(guideMap).toContain("data-map-location-status");
    expect(guideMap).toContain('aria-label="Use current location"');
    expect(guideMap).toContain('aria-disabled="false"');
    expect(guideMap).not.toContain(">Near me</button>");
    expect(guideMap).toContain("locationBoundsForPlaces");
    expect(guideMap).toContain("guideLocationInliers");
    expect(guideMap).toContain("guideLocationBounds");
    expect(guideMap).toContain("const mapIsCenteredOnCurrentLocation = () => {");
    expect(guideMap).toContain("const syncLocationButtonToViewport = () => {");
    expect(guideMap).toContain("runtime.onViewportChange?.(syncLocationButtonToViewport);");
    expect(guideMap).toContain('"Recenter on current location"');
    expect(guideMap).toContain("const commitPendingLocationActions = () => {");
    expect(guideMap).toContain("const scheduleLocationCenter = () => {");
    expect(guideMap).toContain("const flushPendingLocationActions = () => {");
    expect(guideMap).toContain("runtime.setUserLocation(currentLocation);");
    expect(guideMap).toContain("const shouldCenter = pendingLocationCenter;");
    expect(guideMap).toContain("const shouldSort = pendingLocationSortRequest;");
    expect(guideMap).toContain("let pendingLocationCenterAfterPlacesUpdate = false;");
    expect(guideMap).toContain("if (shouldSort) {");
    expect(guideMap).toContain("pendingLocationCenterAfterPlacesUpdate = shouldCenter;");
    expect(guideMap).toContain("if (!shouldSort && shouldCenter) {");
    expect(guideMap).toContain("scheduleLocationCenter();");
    expect(guideMap).toContain("flushPendingLocationActions();");
    expect(guideMap).toContain("(sorted.length - 1) * percentileValue");
    expect(guideMap).not.toContain("Math.ceil(sorted.length * percentileValue) - 1");
    expect(guideMap).toContain("distanceFromGuideCenter <= guideLocationBounds.maxDistanceKm");
    expect(guideMap).toContain("Current location is too far from this guide");
    expect(guideMap).toContain('setLocationStatus("Locating you...")');
    expect(guideMap).toContain(
      "You're outside this guide area, so Near me won't recenter the map.",
    );
    expect(guideMap).toContain('dispatchUserLocation("available")');
    expect(guideMap).toContain('dispatchUserLocation("far")');
    expect(guideMap).toContain('setLocationButtonState("checking", "Checking current location")');
    expect(guideMap).toContain(
      "const isCenteredOnCurrentLocation = mapIsCenteredOnCurrentLocation();",
    );
    expect(guideMap).toContain('isCenteredOnCurrentLocation ? "near" : "located"');
    expect(guideMap).toContain("watchPosition");
    expect(
      guideMap.indexOf(
        'root?.addEventListener("guide:user-location-request", handleUserLocationRequest)',
      ),
    ).toBeLessThan(guideMap.indexOf("await initGoogleRuntime"));
    expect(
      guideMap.indexOf(
        "mapElement.guideLocationBounds = locationBoundsForPlaces(places) ?? undefined;",
      ),
    ).toBeLessThan(
      guideMap.indexOf(
        'root?.addEventListener("guide:user-location-request", handleUserLocationRequest)',
      ),
    );
    expect(guidePage).toContain("const hasMappablePlaces = visiblePlaces.some");
    expect(guidePage).toContain('data-has-mappable-places={hasMappablePlaces ? "true" : "false"}');
    expect(guidePage).toContain(
      '{hasMappablePlaces && <option value="nearby">{siteConfig.guide.sortNearMeLabel}</option>}',
    );
    expect(filters).toContain(
      'const hasMappablePlaces = root.dataset.hasMappablePlaces === "true";',
    );
    expect(filters).toContain("const requestCurrentLocationDirectly = () => {");
    expect(filters).toContain("const applySortSelection = (");
    expect(filters).toContain('if (currentLocation && currentLocationStatus !== "far") {');
    expect(filters).toContain(
      'return "You\'re outside this guide area. Showing curated order instead.";',
    );
    expect(filters).toContain("export function normalizeUserLocationDetail(");
    expect(filters).toContain("return normalizedLocation;");
    expect(filters).toContain('root.addEventListener("guide:sort-request"');
    expect(filters).toContain("navigator.geolocation.getCurrentPosition(");
    expect(filters).toContain("directLocationFallbackTimer = window.setTimeout(() => {");
    expect(guideMap).toContain("let pendingLocationSortRequest = false;");
    expect(guideMap).toContain("const requestNearbySort = () => {");
    expect(guideMap).toContain("if (locationNearGuide && pendingLocationSortRequest) {");
    expect(guideMap).toContain("pendingLocationSortRequest = true;");
    expect(guideMap).toContain("requestNearbySort();");
    expectCssToContain(css, ".map-feedback-slot");
    expectCssToContain(css, '.map-panel[data-collapsed="true"] .map-feedback-slot');
    expectCssToContain(css, ".map-icon-button");
    expectCssToContain(css, '.map-icon-button[aria-disabled="true"]');
    expectCssToContain(css, '.map-icon-button[data-location-state="checking"]');
    expectCssToContain(css, '.map-icon-button[data-location-state="located"]');
    expectCssToContain(css, '.map-icon-button[data-location-state="far"]');
    expectCssToContain(css, ".map-stage");
    expectCssToContain(css, ".map-location-status");
    expectCssToContain(css, '.map-icon-button[data-busy="true"] svg');
  });

  it("renders map pins from normalized place-type icons instead of generic circles", () => {
    const guideMap = readSource("src/components/GuideMap.astro");
    const css = readSource("src/styles/global.css");

    expect(guideMap).toContain("markerIcon: place.marker_icon");
    expect(guideMap).toContain("buildMapMarkerDataUrl");
    expect(guideMap).toContain("buildMapMarkerSvg");
    expect(guideMap).toContain('className: "guide-map-marker"');
    expect(guideMap).not.toContain("google.maps.SymbolPath.CIRCLE");
    expectCssToContain(css, ".guide-map-marker");
    expectCssToContain(css, ".guide-map-marker svg");
    expectCssToContain(css, "width: 100%;");
    expectCssToContain(css, "height: 100%;");
  });

  it("keeps the home guide map event contract in sync with the home browser", () => {
    const homePage = readSource("src/pages/index.astro");
    const homeMap = readSource("src/components/HomeGuideMap.astro");
    const homeBrowser = readSource("public/scripts/home-browser.js");

    expect(homePage).toContain("<HomeGuideMap guides={locationGuideCandidates} />");
    expect(homePage).toContain(
      'data-country-sections-visible={siteConfig.home.showCountrySections ? "true" : "false"}',
    );
    expect(homePage).toContain(
      "{guidesByCountry.map(({ countryName, countryGuides, countryFlag }) => (",
    );
    expect(homeMap).toContain("data-home-guide-map");
    expect(homeMap).toContain('class="home-map-stage"');
    expect(homeMap).toContain("const COMPACT_HOME_MAP_MEDIA_QUERY");
    expect(homeMap).toContain("const homeMapToggleLabel = (collapsed: boolean) => {");
    expect(homeMap).toContain(
      'compactHomeMapMediaQuery.addEventListener("change", syncPanelToggleLabels)',
    );
    expect(homeMap).toContain('data-guides={JSON.stringify(mapGuides).replace(/</g, "\\\\u003c")}');
    expect(homeBrowser).toContain('root.querySelector("[data-home-guide-map]")');
    expect(homeBrowser).toContain(
      'const countrySectionsVisible = root.dataset.countrySectionsVisible !== "false";',
    );
    expect(homeBrowser).toContain(
      "block.hidden = !countrySectionsVisible || blockVisibleGuideCount === 0;",
    );
    expect(homeBrowser).toContain('new CustomEvent("favorite-places:home-map-update"');
    expect(homeMap).toContain(
      'document.addEventListener(\n      "favorite-places:home-map-update"',
    );
    expect(homeMap).toContain("pendingState");
    expect(homeMap).toContain("runtime?.setVisibleGuides(currentVisibleGuideSlugs)");
    expect(homeMap).toContain("runtime?.fitGuides(currentVisibleGuides)");
    expect(homeMap).toContain("applyVisibility(\n      pendingState.visibleGuideSlugs,");
    expect(homeBrowser).toContain("if (!nearbyGuides.isNearMatch) {");
    expect(homeBrowser).toContain("No guides are close to you yet.");
  });

  it("shows the home map reset control after the fitted view drifts", () => {
    const homeMap = readSource("src/components/HomeGuideMap.astro");

    expect(homeMap).toContain("data-home-map-reset");
    expect(homeMap).toContain(
      "const homeFocusViewToleranceKm = (bounds: LatLngBoundsLiteral | null) => {",
    );
    expect(homeMap).toContain(
      "const distanceInKm = (fromLat: number, fromLng: number, toLat: number, toLng: number) => {",
    );
    expect(homeMap).toContain("let focusCenter: Coordinates | null = null;");
    expect(homeMap).toContain("let focusZoom: number | null = null;");
    expect(homeMap).toContain("let focusDistanceToleranceKm = homeFocusViewToleranceKm");
    expect(homeMap).toContain("const captureFocusView = () => {");
    expect(homeMap).toContain(
      "distanceInKm(center.lat, center.lng, focusCenter.lat, focusCenter.lng)",
    );
    expect(homeMap).toContain(
      "distanceInKm(currentCenter.lat, currentCenter.lng, focusCenter.lat, focusCenter.lng)",
    );
    expect(homeMap).toContain("Math.abs(zoom - focusZoom) > 0.75");
    expect(homeMap).toContain("const capFittedZoomAndCapture = () => {");
    expect(homeMap).toContain("if (zoom !== undefined && zoom > focusMaxResetZoom) {");
    expect(homeMap).toContain("map.setZoom(focusMaxResetZoom);");
    expect(homeMap).toContain("if (visibleGuides.length === 0) return;");
    expect(homeMap).toContain("if (focusGuides.length === 0) return;");
    expect(homeMap).toContain("capFittedZoomAndCapture();");
    expect(homeMap).toContain("resetButton.hidden = !runtime?.isOutsideFocusBounds();");
    expect(homeMap).toContain("runtime.onViewChanged(syncResetButton);");
    expect(homeMap).toContain('resetButton?.addEventListener("click", () => {');
    expect(homeMap).toContain("runtime?.resetView();");
    expect(homeMap).toContain('map.addListener("idle", handler);');
    expect(homeMap).not.toContain('map.addListener("bounds_changed", handler);');
  });

  it("constrains map panning before the world scrolls into grey tile space", () => {
    const homeMap = readSource("src/components/HomeGuideMap.astro");
    const guideMap = readSource("src/components/GuideMap.astro");

    expect(homeMap).toContain("const WORLD_MAP_MAX_LATITUDE = 85.05112878");
    expect(homeMap).toContain("const WORLD_MAP_MIN_ZOOM = 0");
    expect(homeMap).toContain("const WORLD_MAP_MIN_LONGITUDE = -180");
    expect(homeMap).toContain("const WORLD_MAP_MAX_LONGITUDE = 180");
    expect(homeMap).toContain("const WORLD_MAP_BOUNDS = {");
    expect(homeMap).toContain("east: WORLD_MAP_MAX_LONGITUDE");
    expect(homeMap).toContain("west: WORLD_MAP_MIN_LONGITUDE");
    expect(homeMap).toContain("const LEAFLET_WORLD_BOUNDS = L.latLngBounds");
    expect(homeMap).toContain("[-WORLD_MAP_MAX_LATITUDE, WORLD_MAP_MIN_LONGITUDE]");
    expect(homeMap).toContain("[WORLD_MAP_MAX_LATITUDE, WORLD_MAP_MAX_LONGITUDE]");
    expect(homeMap).toContain("maxBounds: LEAFLET_WORLD_BOUNDS");
    expect(homeMap).toContain("maxBoundsViscosity: 1");
    expect(homeMap).toContain("minZoom: WORLD_MAP_MIN_ZOOM");
    expect(homeMap).toContain("restriction: {");
    expect(homeMap).toContain("latLngBounds: WORLD_MAP_BOUNDS");
    expect(homeMap).toContain("strictBounds: true");

    expect(guideMap).not.toContain("const WORLD_MAP_MAX_LATITUDE = 85.05112878");
    expect(guideMap).not.toContain("const WORLD_MAP_MIN_ZOOM");
    expect(guideMap).not.toContain("const WORLD_MAP_BOUNDS = {");
    expect(guideMap).not.toContain("const LEAFLET_WORLD_BOUNDS = L.latLngBounds");
    expect(guideMap).not.toContain("maxBounds: LEAFLET_WORLD_BOUNDS");
    expect(guideMap).not.toContain("maxBoundsViscosity: 1");
    expect(guideMap).toContain("const guideFocusBounds = (places: MapPlace[]) => {");
    expect(guideMap).toContain("map.setMaxBounds(leafletBoundsFromLiteral(restrictionBounds))");
    expect(guideMap).toContain("restriction: {");
    expect(guideMap).toContain("latLngBounds: restrictionBounds");
    expect(guideMap).not.toContain("latLngBounds: WORLD_MAP_BOUNDS");
    expect(guideMap).toContain("strictBounds: true");
  });
});
