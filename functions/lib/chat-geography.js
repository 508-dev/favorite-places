function normalized(value) {
  return String(value || "")
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

function coordinates(entry) {
  return (
    typeof entry.lat === "number" &&
    typeof entry.lng === "number" &&
    Number.isFinite(entry.lat) &&
    Number.isFinite(entry.lng) &&
    Math.abs(entry.lat) <= 90 &&
    Math.abs(entry.lng) <= 180
  );
}

function distanceKm(left, right) {
  const radians = (value) => (value * Math.PI) / 180;
  const deltaLat = radians(right.lat - left.lat);
  const deltaLng = radians(right.lng - left.lng);
  const a =
    Math.sin(deltaLat / 2) ** 2 +
    Math.cos(radians(left.lat)) * Math.cos(radians(right.lat)) * Math.sin(deltaLng / 2) ** 2;
  return 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

export function scopeGuidePlaces(entries, intent, guide) {
  const location = normalized(intent.location);
  if (!location) return entries;
  const city = normalized(guide.city);
  const country = normalized(guide.country);
  const cityLocations = new Set([
    city,
    `${city} city`,
    `${city} ${country}`,
    `${city} city ${country}`,
    normalized(guide.title),
  ]);
  if (cityLocations.has(location)) return entries;
  const areaName = (value) =>
    normalized(value)
      .replace(/\b(district|ward|neighborhood|village)\b/g, "")
      .trim();
  const aliases = [
    ...new Set([intent.location, ...(intent.location_alternatives || [])].map(normalized)),
  ].filter((alias) => alias.length >= 2 && !cityLocations.has(alias) && alias !== country);
  const aliasAreas = new Set(aliases.map(areaName));
  const exactAreas = entries.filter((entry) => aliasAreas.has(areaName(entry.neighborhood)));
  const exactSet = new Set(exactAreas);
  const localAliases = aliases.map((alias) => ({
    text: alias,
    local: /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(alias),
  }));
  const mentionsArea = (entry) =>
    [entry.name, entry.address].some((text) => {
      const haystack = normalized(text);
      return localAliases.some((alias) =>
        alias.local ? haystack.includes(alias.text) : ` ${haystack} `.includes(` ${alias.text} `),
      );
    });
  const anchors = entries.filter(
    (entry) => coordinates(entry) && (exactSet.has(entry) || mentionsArea(entry)),
  );
  if (!anchors.length) return exactAreas.map((entry) => ({ ...entry, _chatArea: intent.location }));
  const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
  const center = {
    lat: median(anchors.map((entry) => entry.lat)),
    lng: median(anchors.map((entry) => entry.lng)),
  };
  return entries
    .filter(coordinates)
    .map((entry) => ({
      ...entry,
      _chatArea: intent.location,
      _chatDistanceKm: distanceKm(center, entry),
    }))
    .filter((entry) => entry._chatDistanceKm <= 1.5);
}
