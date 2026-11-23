import { describe, expect, it } from "vitest";
import { scopeGuidePlaces } from "../../functions/lib/chat-geography.js";

const guide = { city: "Taipei", country: "Taiwan", title: "Taipei, Taiwan 🇹🇼" };
const entries = [
  { id: "anchor", name: "Ximen Dumplings", neighborhood: "Ximen", lat: 25.043, lng: 121.507 },
  { id: "near", name: "Nearby Dumplings", neighborhood: "Wanhua", lat: 25.042, lng: 121.509 },
  {
    id: "far",
    name: "Highly Rated Da'an Dumplings",
    neighborhood: "Da’an District",
    lat: 25.035,
    lng: 121.55,
    rating: 4.9,
  },
  { id: "unknown", name: "Unlocated", lat: null, lng: null },
];

describe("chat area constraints", () => {
  it("excludes Da'an before relevance or ratings can promote it into Ximen results", () => {
    const results = scopeGuidePlaces(entries, { location: "Ximen" }, guide);
    expect(results.map((entry) => entry.id)).toEqual(["anchor", "near"]);
    expect(results.every((entry) => entry._chatDistanceKm <= 1.5)).toBe(true);
  });
  it("uses equivalent local names, without treating the whole city as an area anchor", () => {
    expect(
      scopeGuidePlaces(
        entries,
        { location: "西門町", location_alternatives: ["Ximen", "Taipei"] },
        guide,
      ).map((entry) => entry.id),
    ).toEqual(["anchor", "near"]);
  });
  it("uses exact neighborhood membership and leaves city-wide searches unrestricted", () => {
    expect(
      scopeGuidePlaces(entries, { location: "Da'an" }, guide).map((entry) => entry.id),
    ).toEqual(["far"]);
    expect(scopeGuidePlaces(entries, { location: "Taipei" }, guide)).toEqual(entries);
    expect(scopeGuidePlaces(entries, { location: "Unrecognized area" }, guide)).toEqual([]);
  });
});
