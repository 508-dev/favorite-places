import type { MarkerIcon } from "./types";

export interface MapMarkerSvgOptions {
  active?: boolean;
  fillColor: string;
  haloColor: string;
  ringColor?: string;
  strokeColor: string;
  topPick?: boolean;
}

export interface MapMarkerSize {
  anchorX: number;
  anchorY: number;
  height: number;
  popupOffsetY: number;
  width: number;
}

export const MAP_MARKER_ICON_NAMES = [
  "default",
  "cafe",
  "restaurant",
  "bar",
  "bakery",
  "museum",
  "attraction",
  "park",
  "beach",
  "shopping",
  "hotel",
  "spa",
] as const satisfies readonly MarkerIcon[];

export interface MapMarkerPalette {
  fillColor: string;
  haloColor: string;
  strokeColor: string;
}

const MAP_MARKER_PALETTES: Record<MarkerIcon, MapMarkerPalette> = {
  default: {
    fillColor: "#8b95a7",
    haloColor: "#eef2f7",
    strokeColor: "#4b5563",
  },
  cafe: {
    fillColor: "#c46a16",
    haloColor: "#fde3b0",
    strokeColor: "#7c3f10",
  },
  restaurant: {
    fillColor: "#e24d3f",
    haloColor: "#fde0d7",
    strokeColor: "#8f241c",
  },
  bar: {
    fillColor: "#8b5cf6",
    haloColor: "#ede9fe",
    strokeColor: "#4c1d95",
  },
  bakery: {
    fillColor: "#f59e0b",
    haloColor: "#fef3c7",
    strokeColor: "#92400e",
  },
  museum: {
    fillColor: "#3b82f6",
    haloColor: "#dbeafe",
    strokeColor: "#1d4ed8",
  },
  attraction: {
    fillColor: "#ec4899",
    haloColor: "#fce7f3",
    strokeColor: "#9d174d",
  },
  park: {
    fillColor: "#16a34a",
    haloColor: "#dcfce7",
    strokeColor: "#166534",
  },
  beach: {
    fillColor: "#06b6d4",
    haloColor: "#cffafe",
    strokeColor: "#155e75",
  },
  shopping: {
    fillColor: "#db2777",
    haloColor: "#fce7f3",
    strokeColor: "#9d174d",
  },
  hotel: {
    fillColor: "#6366f1",
    haloColor: "#e0e7ff",
    strokeColor: "#3730a3",
  },
  spa: {
    fillColor: "#14b8a6",
    haloColor: "#ccfbf1",
    strokeColor: "#115e59",
  },
};

const MARKER_LABELS: Record<MarkerIcon, string> = {
  default: "Saved place",
  cafe: "Cafe",
  restaurant: "Restaurant",
  bar: "Bar",
  bakery: "Bakery",
  museum: "Museum",
  attraction: "Tourist attraction",
  park: "Park",
  beach: "Beach",
  shopping: "Shopping",
  hotel: "Hotel",
  spa: "Spa",
};

const MARKER_SYMBOLS: Record<MarkerIcon, string> = {
  default: `
    <circle cx="12" cy="12" r="2.4" />
    <path d="M12 5.75v1.65M12 16.6v1.65M5.75 12h1.65M16.6 12h1.65M7.55 7.55l1.2 1.2M15.25 15.25l1.2 1.2M16.45 7.55l-1.2 1.2M8.75 15.25l-1.2 1.2" />
  `,
  cafe: `
    <path d="M7.25 10.25h7.5v4.4a2.6 2.6 0 0 1-2.6 2.6H9.85a2.6 2.6 0 0 1-2.6-2.6v-4.4Z" />
    <path d="M14.75 11.35h1.15a1.75 1.75 0 1 1 0 3.5h-1.15" />
    <path d="M9.25 7.25c.7.55.7 1.35 0 1.9" />
    <path d="M12.15 7.25c.7.55.7 1.35 0 1.9" />
  `,
  restaurant: `
    <path d="M7.25 6.75v3.85a1.45 1.45 0 0 0 2.9 0V6.75" />
    <path d="M8.7 6.75v10.5" />
    <path d="M14.9 6.75v10.5" />
    <path d="M16.75 6.75c0 2.65-.7 4.35-1.85 5.1" />
  `,
  bar: `
    <path d="M6.75 8.25h10.5l-4.45 4.7v4.15" />
    <path d="M10.25 17.1h4.1" />
  `,
  bakery: `
    <path d="M7 14.55c0-3.05 2.45-5.55 5.5-5.55 2.55 0 4.75 1.7 5.35 4.05.95.1 1.65.7 1.65 1.9v2.3H7v-2.7Z" />
    <path d="M10.2 12.4c.6-.8 1.45-1.4 2.4-1.75" />
  `,
  museum: `
    <path d="M6.5 10.1 12 7l5.5 3.1" />
    <path d="M7.4 10.35h9.2" />
    <path d="M8.75 10.35v6" />
    <path d="M12 10.35v6" />
    <path d="M15.25 10.35v6" />
    <path d="M6.75 16.75h10.5" />
  `,
  attraction: `
    <path d="m12 6.75 1.65 3.35 3.7.55-2.7 2.65.65 3.7L12 15.25 8.7 17l.65-3.7-2.7-2.65 3.7-.55L12 6.75Z" />
  `,
  park: `
    <path d="M12 7c1.85 0 3.25 1.45 3.25 3.25 1.6.25 2.75 1.65 2.75 3.35A3.4 3.4 0 0 1 14.6 17H9.4A3.4 3.4 0 0 1 6 13.6c0-1.7 1.15-3.1 2.75-3.35C8.75 8.45 10.15 7 12 7Z" />
    <path d="M12 17v3" />
  `,
  beach: `
    <path d="M7 16.75h10" />
    <path d="M12 7.25v9.5" />
    <path d="M12 7.25c2.4 0 4.3 1.3 5.25 3.5-1.4.85-3.15 1.3-5.25 1.3" />
    <path d="M10.1 18.35c.7-.9 1.65-1.35 2.9-1.35 1.3 0 2.25.45 2.95 1.35" />
  `,
  shopping: `
    <path d="M8 10.25h8l-.7 7.5H8.7L8 10.25Z" />
    <path d="M10.2 10.25V9a1.8 1.8 0 0 1 3.6 0v1.25" />
  `,
  hotel: `
    <path d="M7 10.75v5.75" />
    <path d="M7 12.5h10" />
    <path d="M10 12.5v-1.2a1.8 1.8 0 0 1 1.8-1.8h1.65A2.55 2.55 0 0 1 16 12.05v4.45" />
    <path d="M7 16.5h10" />
  `,
  spa: `
    <path d="M7 14.25c1.05-1.2 2.1-1.8 3.2-1.8 1.05 0 1.95.35 2.75 1 .8.65 1.6.95 2.35.95.95 0 1.9-.4 2.9-1.15" />
    <path d="M8.5 10.1c.45-.9 1.2-1.7 2.2-2.2" />
    <path d="M12.35 9.25c.35-.75.95-1.4 1.8-1.95" />
  `,
};

const escapeXml = (value: string) =>
  value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

export const getMapMarkerLabel = (icon: MarkerIcon) => MARKER_LABELS[icon];

export const getMapMarkerColors = (
  icon: MarkerIcon,
  { active = false, topPick = false } = {},
): MapMarkerSvgOptions => {
  const palette = MAP_MARKER_PALETTES[icon];

  if (active) {
    return {
      ...palette,
      ringColor: topPick ? "#fde68a" : "#ffffff",
      topPick,
      active,
    };
  }

  return {
    ...palette,
    ringColor: topPick ? "#fde68a" : undefined,
    topPick,
    active,
  };
};

export const getMapMarkerSize = (
  _icon: MarkerIcon,
  topPick = false,
  active = false,
): MapMarkerSize => {
  const scale = active ? (topPick ? 1.16 : 1.1) : topPick ? 1.08 : 1;
  const width = Math.round(34 * scale);
  const height = Math.round(34 * scale);

  return {
    width,
    height,
    anchorX: Math.round(width / 2),
    anchorY: Math.round(height / 2),
    popupOffsetY: Math.round(height / 2) + 6,
  };
};

// Keep the shadow silhouette aligned with the badge and its selection/top-pick ring.
const markerGeometry = (active = false, topPick = false) => {
  const outerRadius = active ? (topPick ? 16.25 : 15.35) : topPick ? 15.55 : 14.9;
  return {
    outerRadius,
    ringRadius: active ? outerRadius + 1.8 : topPick ? outerRadius + 1.35 : 0,
    strokeWidth: active ? 2.5 : 2.15,
  };
};

// The extra 16 SVG units on each side keep the blur inside the image bounds.
export const getMapMarkerShadowSize = (size: MapMarkerSize) => ({
  width: size.width * 1.8,
  height: size.height * 1.8,
  anchorX: size.anchorX + size.width * 0.4,
  anchorY: size.anchorY + size.height * 0.4,
});

export const buildMapMarkerShadowDataUrl = ({
  active = false,
  topPick = false,
}: Pick<MapMarkerSvgOptions, "active" | "topPick"> = {}): string => {
  const { outerRadius, ringRadius, strokeWidth } = markerGeometry(active, topPick);
  const radius = Math.max(outerRadius + strokeWidth / 2, ringRadius);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="-16 -16 72 72" width="72" height="72" fill="none">
    <defs><filter id="shadow" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="4" /></filter></defs>
    <circle cx="20" cy="25" r="${radius}" fill="#110c08" fill-opacity="0.22" filter="url(#shadow)" />
    <circle cx="20" cy="22" r="${radius}" fill="#ffffff" fill-opacity="0.92" />
  </svg>`;
  return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`;
};

export const buildMapMarkerSvg = (icon: MarkerIcon, options: MapMarkerSvgOptions): string => {
  const { active = false, fillColor, haloColor, ringColor, strokeColor, topPick = false } = options;
  const label = escapeXml(getMapMarkerLabel(icon));
  const { outerRadius, ringRadius, strokeWidth } = markerGeometry(active, topPick);

  return `
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" width="40" height="40" fill="none" aria-hidden="true">
      <title>${label}</title>
      ${ringColor ? `<circle cx="20" cy="20" r="${ringRadius}" fill="${escapeXml(ringColor)}" fill-opacity="${active ? "0.98" : "0.95"}" />` : ""}
      <circle cx="20" cy="20" r="${outerRadius}" fill="${escapeXml(fillColor)}" stroke="${escapeXml(strokeColor)}" stroke-width="${strokeWidth}" />
      <circle cx="20" cy="20" r="${Math.max(outerRadius - 5.75, 7.25)}" fill="${escapeXml(haloColor)}" fill-opacity="${active ? "0.22" : "0.18"}" />
      <g transform="translate(8 8)" stroke="#ffffff" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
        ${MARKER_SYMBOLS[icon]}
      </g>
    </svg>
  `.trim();
};

export const buildMapMarkerDataUrl = (icon: MarkerIcon, options: MapMarkerSvgOptions): string =>
  `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(buildMapMarkerSvg(icon, options))}`;
