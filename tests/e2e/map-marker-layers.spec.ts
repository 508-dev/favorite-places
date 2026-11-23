import { expect, test } from "@playwright/test";

test("crowded pin shadows stay below every pin and follow selection, pan and filtering", async ({
  page,
}) => {
  await page.goto("/guides/tokyo-japan/");
  const icons = page.locator(".leaflet-marker-icon.guide-map-marker");
  const shadows = page.locator(".leaflet-marker-shadow.guide-map-marker");
  await expect(icons.first()).toBeVisible();
  const originalCount = await icons.count();
  expect(originalCount).toBeGreaterThan(1);
  await expect(shadows).toHaveCount(originalCount);

  await page.locator("[data-guide-map]").evaluate((element) => {
    type Marker = {
      constructor: new (position: unknown, options: unknown) => Marker;
      options: unknown;
      getElement: () => HTMLElement;
      setLatLng: (position: unknown) => Marker;
      addTo: (map: unknown) => Marker;
      remove: () => void;
      fire: (event: string) => void;
    };
    const map = (
      element as HTMLElement & {
        guideMap: {
          eachLayer: (callback: (layer: unknown) => void) => void;
          getCenter: () => unknown;
          latLngToContainerPoint: (position: unknown) => { x: number; y: number };
          containerPointToLatLng: (point: number[]) => unknown;
        };
      }
    ).guideMap;
    const markers: Marker[] = [];
    map.eachLayer((layer) => {
      const marker = layer as Marker;
      if (marker.getElement?.()?.matches(".guide-map-marker")) markers.push(marker);
    });
    const center = map.latLngToContainerPoint(map.getCenter());
    const clones: Marker[] = [];
    for (let i = 0; i < 36; i++) {
      const source = markers[i % markers.length];
      const position = map.containerPointToLatLng([
        center.x + (i % 6) * 6,
        center.y + Math.floor(i / 6) * 6,
      ]);
      clones.push(new source.constructor(position, source.options).addTo(map));
    }
    (element as HTMLElement & { testClones: Marker[]; testMarkers: Marker[] }).testClones = clones;
    (element as HTMLElement & { testMarkers: Marker[] }).testMarkers = markers;
  });
  await expect(icons).toHaveCount(originalCount + 36);
  await expect(shadows).toHaveCount(originalCount + 36);
  await expect
    .poll(() =>
      icons.evaluateAll((elements) =>
        elements.every(
          (element) =>
            (element as HTMLImageElement).complete &&
            (element as HTMLImageElement).naturalWidth > 0,
        ),
      ),
    )
    .toBe(true);
  const layers = await page.locator("[data-guide-map]").evaluate((element) => {
    const marker = element.querySelector(".leaflet-marker-pane")!;
    const shadow = element.querySelector(".leaflet-shadow-pane")!;
    return {
      marker: Number(getComputedStyle(marker).zIndex),
      shadow: Number(getComputedStyle(shadow).zIndex),
      filters: [...marker.querySelectorAll(".guide-map-marker")].map(
        (pin) => getComputedStyle(pin).filter,
      ),
    };
  });
  expect(layers.shadow).toBeLessThan(layers.marker);
  expect(layers.filters.every((filter) => filter === "none")).toBe(true);
  await page
    .locator("[data-guide-map]")
    .screenshot({ path: ".context/qa/crowded-map-shadows.png" });

  await page.locator("[data-guide-map]").evaluate((element) => {
    const target = element as HTMLElement & {
      testClones: { remove: () => void }[];
      testMarkers: { fire: (event: string) => void }[];
      guideMap: { panBy: (offset: number[], options: { animate: boolean }) => void };
    };
    target.testClones.forEach((clone) => {
      clone.remove();
    });
    target.testMarkers[0].fire("click");
    target.guideMap.panBy([20, 15], { animate: false });
  });
  await expect(icons).toHaveCount(originalCount);
  await expect(shadows).toHaveCount(originalCount);
  const aligned = await icons.evaluateAll((elements) => {
    const shadowBounds = [
      ...document.querySelectorAll(".leaflet-marker-shadow.guide-map-marker"),
    ].map((shadow) => shadow.getBoundingClientRect());
    return elements.every((element) => {
      const pin = element.getBoundingClientRect();
      return shadowBounds.some(
        (shadow) =>
          Math.abs(shadow.width - pin.width * 1.8) < 0.1 &&
          Math.abs(shadow.x + shadow.width / 2 - pin.x - pin.width / 2) < 0.1 &&
          Math.abs(shadow.y + shadow.height / 2 - pin.y - pin.height / 2) < 0.1,
      );
    });
  });
  expect(aligned).toBe(true);
  await page.evaluate(() => {
    const id = document.querySelector<HTMLElement>("[data-place-card]")!.dataset.placeId!;
    document.dispatchEvent(
      new CustomEvent("guide:places-updated", {
        detail: { source: "search", visiblePlaceIds: [id] },
      }),
    );
  });
  await expect(icons).toHaveCount(1);
  await expect(shadows).toHaveCount(1);
});
