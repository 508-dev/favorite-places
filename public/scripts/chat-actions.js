export const APPLY_FILTERS_ACTION = "guide.apply-filters";

export function normalizeFilterTag(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function createFilterAction(proposal, { guideSlug, tags = [], scope, placeIds = [] }) {
  if (!proposal || typeof proposal !== "object" || Array.isArray(proposal)) return null;
  const query = proposal.query ?? "";
  const proposedTags = proposal.tags ?? [];
  const minRating = proposal.min_rating ?? 0;
  const allowedTags = new Set(tags.map(normalizeFilterTag));
  if (
    scope &&
    (typeof scope.label !== "string" ||
      scope.label.length > 80 ||
      !Array.isArray(scope.place_ids) ||
      !scope.place_ids.length ||
      scope.place_ids.length > 1000 ||
      scope.place_ids.some((id) => typeof id !== "string" || !placeIds.includes(id)))
  )
    return null;
  if (
    typeof query !== "string" ||
    query.length > 100 ||
    /[\r\n]/.test(query) ||
    !Array.isArray(proposedTags) ||
    proposedTags.length > 4 ||
    proposedTags.some((tag) => typeof tag !== "string" || !allowedTags.has(tag)) ||
    ![0, 4.5, 4.7, 4.8].includes(minRating)
  )
    return null;
  const filters = { query: query.trim(), tags: [...new Set(proposedTags)], min_rating: minRating };
  if (!filters.query && !filters.tags.length && !minRating && !scope) return null;
  return {
    id: APPLY_FILTERS_ACTION,
    version: 1,
    guide_slug: guideSlug,
    filters,
    ...(scope ? { scope: { label: scope.label, place_ids: [...new Set(scope.place_ids)] } } : {}),
  };
}

export function filterActionLabels(filters) {
  return [
    filters.query ? `Search: ${filters.query}` : null,
    ...filters.tags.map((tag) => `#${tag}`),
    filters.min_rating ? `${filters.min_rating}+ stars` : null,
  ].filter(Boolean);
}
