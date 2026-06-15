import { loadSearchIndex, searchPlaces } from "./place-search.js";

function getTagComparisonValue(value) {
  const normalizedText = String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ");
  const slug = normalizedText.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

  return slug || normalizedText;
}

function parseCardTagValues(value) {
  if (!value) {
    return [];
  }

  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed)
      ? parsed.map((item) => getTagComparisonValue(item)).filter(Boolean)
      : [];
  } catch {
    return String(value)
      .split(/\s+/)
      .map((item) => getTagComparisonValue(item))
      .filter(Boolean);
  }
}

function toRadians(degrees) {
  return (degrees * Math.PI) / 180;
}

function distanceInKm(fromLat, fromLng, toLat, toLng) {
  const earthRadiusKm = 6371;
  const latDelta = toRadians(toLat - fromLat);
  const lngDelta = toRadians(toLng - fromLng);
  const fromLatRadians = toRadians(fromLat);
  const toLatRadians = toRadians(toLat);
  const a =
    Math.sin(latDelta / 2) ** 2 +
    Math.cos(fromLatRadians) * Math.cos(toLatRadians) * Math.sin(lngDelta / 2) ** 2;
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));

  return earthRadiusKm * c;
}

function getCardCoordinates(card) {
  const lat = card.dataset.lat ? Number(card.dataset.lat) : Number.NaN;
  const lng = card.dataset.lng ? Number(card.dataset.lng) : Number.NaN;

  return Number.isFinite(lat) && Number.isFinite(lng) ? { lat, lng } : null;
}

export function buildNearbyDistanceMap(cards, currentLocation) {
  const distances = new Map();
  if (!currentLocation) {
    return distances;
  }

  cards.forEach((card) => {
    const coordinates = getCardCoordinates(card);
    if (!coordinates) {
      return;
    }

    distances.set(
      card.dataset.placeId || "",
      distanceInKm(currentLocation.lat, currentLocation.lng, coordinates.lat, coordinates.lng),
    );
  });

  return distances;
}

export function compareCardsByCurated(left, right) {
  const leftFeatured = left.dataset.featured === "true" ? 1 : 0;
  const rightFeatured = right.dataset.featured === "true" ? 1 : 0;
  if (leftFeatured !== rightFeatured) return rightFeatured - leftFeatured;
  const leftBestHit = left.dataset.bestHit === "true" ? 1 : 0;
  const rightBestHit = right.dataset.bestHit === "true" ? 1 : 0;
  if (leftBestHit !== rightBestHit) return rightBestHit - leftBestHit;
  const leftTopPick = left.dataset.topPick === "true" ? 1 : 0;
  const rightTopPick = right.dataset.topPick === "true" ? 1 : 0;
  if (leftTopPick !== rightTopPick) return rightTopPick - leftTopPick;
  return (
    Number(right.dataset.rank || 0) - Number(left.dataset.rank || 0) ||
    (left.dataset.name || "").localeCompare(right.dataset.name || "")
  );
}

export function compareCardsByNearby(
  left,
  right,
  { currentLocation = null, distanceByPlaceId = new Map() } = {},
) {
  if (!currentLocation) {
    return compareCardsByCurated(left, right);
  }

  const leftDistance = distanceByPlaceId.get(left.dataset.placeId || "");
  const rightDistance = distanceByPlaceId.get(right.dataset.placeId || "");
  const leftHasDistance = Number.isFinite(leftDistance);
  const rightHasDistance = Number.isFinite(rightDistance);

  if (leftHasDistance !== rightHasDistance) {
    return leftHasDistance ? -1 : 1;
  }

  if (!leftHasDistance || !rightHasDistance) {
    return compareCardsByCurated(left, right);
  }

  return leftDistance - rightDistance || compareCardsByCurated(left, right);
}

export function compareCardsByNeighborhood(left, right) {
  return (
    (left.dataset.neighborhood || "").localeCompare(right.dataset.neighborhood || "") ||
    (left.dataset.name || "").localeCompare(right.dataset.name || "")
  );
}

export function resolveLocationSortState({
  fallbackMessage = "Location unavailable. Showing curated order instead.",
  fallbackSortValue = "curated",
  currentLocation = null,
  currentLocationStatus = "idle",
  sortValue,
} = {}) {
  const shouldFallback =
    sortValue === "nearby" &&
    (currentLocationStatus === "far" ||
      (!currentLocation &&
        currentLocationStatus !== "idle" &&
        currentLocationStatus !== "checking"));

  if (!shouldFallback) {
    return {
      message: "",
      shouldFallback: false,
      sortValue,
    };
  }

  return {
    message: fallbackMessage,
    shouldFallback: true,
    sortValue: fallbackSortValue,
  };
}

export function locationFallbackMessage(status, fallbackMessage) {
  if (status === "denied") {
    return "Location permission was denied. Showing curated order instead.";
  }
  if (status === "far") {
    return "You're outside this guide area. Showing curated order instead.";
  }
  return fallbackMessage;
}

export function normalizeUserLocationDetail({ coordinates = null } = {}) {
  const normalizedLocation =
    coordinates &&
    Number.isFinite(Number(coordinates.lat)) &&
    Number.isFinite(Number(coordinates.lng))
      ? {
          lat: Number(coordinates.lat),
          lng: Number(coordinates.lng),
        }
      : null;

  return normalizedLocation;
}
export function cardHasTag(card, tag) {
  const normalizedTag = getTagComparisonValue(tag);
  if (!normalizedTag) {
    return true;
  }

  return [
    ...parseCardTagValues(card.dataset.tags),
    ...parseCardTagValues(card.dataset.vibeTags),
  ].includes(normalizedTag);
}

export function matchesCardSearch(card, { normalizedQuery = "", searchResultIds = null } = {}) {
  return searchResultIds
    ? searchResultIds.has(card.dataset.placeId || "")
    : !normalizedQuery || (card.dataset.search || "").includes(normalizedQuery);
}

function getCardAreaComparisonValues(card) {
  const neighborhoodValue = (
    card.dataset.neighborhoodToken || getTagComparisonValue(card.dataset.neighborhood || "")
  ).replace(/-(?:city|ward|district|borough|county|prefecture|province|gu|ku)$/, "");
  const localityPathValues = parseCardTagValues(card.dataset.localityPath);
  const broaderLocalityValue = localityPathValues[1]
    ? `broader-${localityPathValues[1].replace(/-(?:city|ward|district|borough|county|prefecture|province|gu|ku)$/, "")}`
    : "";
  return [
    ...new Set([
      ...(neighborhoodValue ? [neighborhoodValue] : []),
      ...(broaderLocalityValue ? [broaderLocalityValue] : []),
    ]),
  ];
}

export function cardMatchesType(card, { activeTypeValue = "", activeTypeSeedValues = [] } = {}) {
  const normalizedType = getTagComparisonValue(activeTypeValue);
  const normalizedCardCategory = getTagComparisonValue(card.dataset.category || "");

  return (
    !normalizedType ||
    normalizedCardCategory === normalizedType ||
    activeTypeSeedValues.some((seedTag) => cardHasTag(card, seedTag))
  );
}

function budgetSelectionKey(kind, tier) {
  return kind && tier ? `${kind}:${tier}` : "";
}

export function toggleBudgetSelection(activeBudgetSelections = [], selectionKey = "") {
  if (!selectionKey) {
    return activeBudgetSelections;
  }
  if (activeBudgetSelections.includes(selectionKey)) {
    return activeBudgetSelections.filter((selection) => selection !== selectionKey);
  }
  const [budgetKind] = selectionKey.split(":");
  return [
    ...activeBudgetSelections.filter((selection) => selection.startsWith(`${budgetKind}:`)),
    selectionKey,
  ];
}

export function cardMatchesBudget(card, { activeBudgetSelections = [] } = {}) {
  if (activeBudgetSelections.length === 0) {
    return true;
  }

  return activeBudgetSelections.includes(
    budgetSelectionKey(card.dataset.budgetKind || "", card.dataset.budgetTier || ""),
  );
}

export function cardMatchesRating(card, { activeMinRating = 0 } = {}) {
  const minRating = Number(activeMinRating) || 0;
  if (minRating <= 0) {
    return true;
  }

  const rating = Number(card.dataset.rating || 0);
  return Number.isFinite(rating) && rating >= minRating;
}

export function cardMatchesReviewCount(card, { activeMinReviews = 0 } = {}) {
  const minReviews = Number(activeMinReviews) || 0;
  if (minReviews <= 0) {
    return true;
  }

  const reviewCount = Number(card.dataset.ratingCount || 0);
  return Number.isFinite(reviewCount) && reviewCount >= minReviews;
}

export function cardMatchesFilters(
  card,
  {
    activeAreaValue = "",
    activeBudgetSelections = [],
    activeMinRating = 0,
    activeMinReviews = 0,
    activeTypeValue = "",
    activeTypeSeedValues = [],
    mapFramePlaceIds = null,
    normalizedQuery = "",
    searchResultIds = null,
    selectedTagValues = [],
  } = {},
) {
  const matchesSearch = matchesCardSearch(card, { normalizedQuery, searchResultIds });
  const matchesArea =
    !activeAreaValue || getCardAreaComparisonValues(card).includes(activeAreaValue);
  const matchesMapFrame = !mapFramePlaceIds || mapFramePlaceIds.has(card.dataset.placeId || "");
  const matchesSelectedTags = selectedTagValues.every((tag) => cardHasTag(card, tag));
  const matchesType = cardMatchesType(card, {
    activeTypeValue,
    activeTypeSeedValues,
  });
  const matchesBudget = cardMatchesBudget(card, {
    activeBudgetSelections,
  });
  const matchesRating = cardMatchesRating(card, { activeMinRating });
  const matchesReviewCount = cardMatchesReviewCount(card, { activeMinReviews });
  return (
    matchesSearch &&
    matchesArea &&
    matchesMapFrame &&
    matchesSelectedTags &&
    matchesType &&
    matchesBudget &&
    matchesRating &&
    matchesReviewCount
  );
}

export function countMatchingCards(
  cards,
  {
    activeArea = "",
    activeBudgetSelections = [],
    activeMinRating = 0,
    activeMinReviews = 0,
    activeTypeValue = "",
    activeTypeSeedValues = [],
    mapFramePlaceIds = null,
    normalizedQuery = "",
    searchResultIds = null,
    selectedTagValues = [],
    tag = "",
  } = {},
) {
  const normalizedActiveArea = getTagComparisonValue(activeArea);
  const nextSelectedTags = tag ? [...selectedTagValues, tag] : selectedTagValues;

  return cards.filter((card) =>
    cardMatchesFilters(card, {
      activeAreaValue: normalizedActiveArea,
      activeBudgetSelections,
      activeMinRating,
      activeMinReviews,
      activeTypeValue,
      activeTypeSeedValues,
      mapFramePlaceIds,
      normalizedQuery,
      searchResultIds,
      selectedTagValues: nextSelectedTags,
    }),
  ).length;
}

export function countAreaOptionCards(
  cards,
  {
    activeTypeValue = "",
    activeTypeSeedValues = [],
    activeBudgetSelections = [],
    activeMinRating = 0,
    activeMinReviews = 0,
    mapFramePlaceIds = null,
    normalizedQuery = "",
    searchResultIds = null,
    selectedTagValues = [],
  } = {},
  areaValue = "",
) {
  return countMatchingCards(cards, {
    activeArea: areaValue,
    activeBudgetSelections,
    activeMinRating,
    activeMinReviews,
    activeTypeValue,
    activeTypeSeedValues,
    mapFramePlaceIds,
    normalizedQuery,
    searchResultIds,
    selectedTagValues,
  });
}

export function countTypeOptionCards(
  cards,
  {
    activeArea = "",
    activeBudgetSelections = [],
    activeMinRating = 0,
    activeMinReviews = 0,
    mapFramePlaceIds = null,
    normalizedQuery = "",
    searchResultIds = null,
    selectedTagValues = [],
  } = {},
  { typeValue = "", typeSeedValues = [] } = {},
) {
  return countMatchingCards(cards, {
    activeArea,
    activeBudgetSelections,
    activeMinRating,
    activeMinReviews,
    activeTypeValue: typeValue,
    activeTypeSeedValues: typeSeedValues,
    mapFramePlaceIds,
    normalizedQuery,
    searchResultIds,
    selectedTagValues,
  });
}

export function countTagOptionCards(
  cards,
  {
    activeArea = "",
    activeBudgetSelections = [],
    activeMinRating = 0,
    activeMinReviews = 0,
    activeTypeValue = "",
    activeTypeSeedValues = [],
    mapFramePlaceIds = null,
    normalizedQuery = "",
    searchResultIds = null,
    selectedTagValues = [],
  } = {},
  tag = "",
) {
  return countMatchingCards(cards, {
    activeArea,
    activeBudgetSelections,
    activeMinRating,
    activeMinReviews,
    activeTypeValue,
    activeTypeSeedValues,
    mapFramePlaceIds,
    normalizedQuery,
    searchResultIds,
    selectedTagValues,
    tag,
  });
}

export function countBudgetOptionCards(
  cards,
  {
    activeArea = "",
    activeTypeValue = "",
    activeTypeSeedValues = [],
    activeMinRating = 0,
    activeMinReviews = 0,
    mapFramePlaceIds = null,
    normalizedQuery = "",
    searchResultIds = null,
    selectedTagValues = [],
  } = {},
  { budgetKind = "", budgetTier = "" } = {},
) {
  return countMatchingCards(cards, {
    activeArea,
    activeBudgetSelections: budgetSelectionKey(budgetKind, budgetTier)
      ? [budgetSelectionKey(budgetKind, budgetTier)]
      : [],
    activeMinRating,
    activeMinReviews,
    activeTypeValue,
    activeTypeSeedValues,
    mapFramePlaceIds,
    normalizedQuery,
    searchResultIds,
    selectedTagValues,
  });
}

export function countRatingOptionCards(
  cards,
  {
    activeArea = "",
    activeBudgetSelections = [],
    activeMinReviews = 0,
    activeTypeValue = "",
    activeTypeSeedValues = [],
    mapFramePlaceIds = null,
    normalizedQuery = "",
    searchResultIds = null,
    selectedTagValues = [],
  } = {},
  minRating = 0,
) {
  return countMatchingCards(cards, {
    activeArea,
    activeBudgetSelections,
    activeMinRating: minRating,
    activeMinReviews,
    activeTypeValue,
    activeTypeSeedValues,
    mapFramePlaceIds,
    normalizedQuery,
    searchResultIds,
    selectedTagValues,
  });
}

export function countReviewOptionCards(
  cards,
  {
    activeArea = "",
    activeBudgetSelections = [],
    activeMinRating = 0,
    activeTypeValue = "",
    activeTypeSeedValues = [],
    mapFramePlaceIds = null,
    normalizedQuery = "",
    searchResultIds = null,
    selectedTagValues = [],
  } = {},
  minReviews = 0,
) {
  return countMatchingCards(cards, {
    activeArea,
    activeBudgetSelections,
    activeMinRating,
    activeMinReviews: minReviews,
    activeTypeValue,
    activeTypeSeedValues,
    mapFramePlaceIds,
    normalizedQuery,
    searchResultIds,
    selectedTagValues,
  });
}

export function sortFilterOptions(options) {
  return [...options].sort((left, right) => {
    if (Boolean(left.pinned) !== Boolean(right.pinned)) {
      return left.pinned ? -1 : 1;
    }

    const leftHasMatches = left.count > 0;
    const rightHasMatches = right.count > 0;
    if (leftHasMatches !== rightHasMatches) {
      return leftHasMatches ? -1 : 1;
    }

    if (left.count !== right.count) {
      return right.count - left.count;
    }

    return left.originalIndex - right.originalIndex;
  });
}

export function buildAreaFilterStatusMessage({
  activeAreaLabel,
  hasAdditionalFilters = false,
  visibleCount,
  overflowCount,
}) {
  if (!hasAdditionalFilters || !activeAreaLabel || overflowCount <= 0 || visibleCount <= 0) {
    return "";
  }

  return `Showing ${visibleCount} place${visibleCount === 1 ? "" : "s"} in ${activeAreaLabel}. ${overflowCount} more match${overflowCount === 1 ? "" : "es"} elsewhere in this guide.`;
}

export function hasAdditionalGuideFilters({
  activeType = "",
  activeTypeValue = "",
  activeBudgetSelections = [],
  activeMinRating = 0,
  activeMinReviews = 0,
  mapFramePlaceIds = null,
  normalizedQuery = "",
  selectedTags = [],
  selectedTagValues = [],
  totalCardCount = 0,
} = {}) {
  const nextActiveType = activeTypeValue || activeType;
  const nextSelectedTags = selectedTagValues.length > 0 ? selectedTagValues : selectedTags;

  if (
    Boolean(normalizedQuery) ||
    Boolean(nextActiveType) ||
    activeBudgetSelections.length > 0 ||
    Number(activeMinRating) > 0 ||
    Number(activeMinReviews) > 0 ||
    nextSelectedTags.length > 0
  ) {
    return true;
  }

  return Boolean(
    mapFramePlaceIds && (totalCardCount <= 0 || mapFramePlaceIds.size !== totalCardCount),
  );
}

export function getInitialSelectedTags({ allTags = [], params = new URLSearchParams() } = {}) {
  const allowedTags = new Set(
    allTags.map((tag) => getTagComparisonValue(String(tag || ""))).filter(Boolean),
  );
  const selectedTags = [];
  const seen = new Set();

  const rawTags = params
    .getAll("tag")
    .flatMap((value) => String(value || "").split(","))
    .map((value) => getTagComparisonValue(value))
    .filter(Boolean);

  for (const tag of rawTags) {
    if (!allowedTags.has(tag) || seen.has(tag)) {
      continue;
    }

    seen.add(tag);
    selectedTags.push(tag);
  }

  return selectedTags;
}

export function buildEmptyStateMessage({ activeAreaLabel = "", overflowCount = 0, query = "" }) {
  const matchWord = overflowCount === 1 ? "match" : "matches";

  if (query && activeAreaLabel && overflowCount > 0) {
    return `No places matched "${query}" in ${activeAreaLabel}. ${overflowCount} more ${matchWord} elsewhere in this guide. Try another area or clear the area filter.`;
  }
  if (query && activeAreaLabel) {
    return `No places matched "${query}" in ${activeAreaLabel}. Try another area or clear the area filter.`;
  }
  if (activeAreaLabel && overflowCount > 0) {
    return `No matches in ${activeAreaLabel}. ${overflowCount} more ${matchWord} elsewhere in this guide. Try another area or clear the area filter.`;
  }
  if (query) {
    return `No places matched "${query}" in this guide. Try a broader search or clear filters.`;
  }
  if (activeAreaLabel) {
    return `No matches in ${activeAreaLabel}. Try another area or clear the area filter.`;
  }
  return "No matches. Try a broader search or clear the tag filter.";
}

const root = typeof document === "undefined" ? null : document.querySelector("[data-guide-root]");

if (root) {
  const cards = Array.from(root.querySelectorAll("[data-place-card]"));
  const list = root.querySelector("[data-place-list]");
  const searchInput = root.querySelector("[data-search-input]");
  const sortSelect = root.querySelector("[data-sort-select]");
  const areaButtons = Array.from(root.querySelectorAll("[data-area-filter]"));
  const typeButtons = Array.from(root.querySelectorAll("[data-type-filter]"));
  const budgetButtons = Array.from(root.querySelectorAll("[data-budget-filter]"));
  const budgetClearButtons = Array.from(root.querySelectorAll("[data-budget-clear-kind]"));
  const budgetSummary = root.querySelector("[data-budget-summary]");
  const ratingButtons = Array.from(root.querySelectorAll("[data-rating-filter]"));
  const reviewButtons = Array.from(root.querySelectorAll("[data-review-filter]"));
  const selectedTagsRow = root.querySelector("[data-selected-tags]");
  const suggestionList = root.querySelector("[data-suggestion-list]");
  const suggestionGroup = suggestionList?.closest(".control-group") || null;
  const autocomplete = root.querySelector("[data-tag-autocomplete]");
  const resultsCount = root.querySelector("[data-results-count]");
  const emptyState = root.querySelector("[data-empty-state]");
  const areaFilterStatus = root.querySelector("[data-area-filter-status]");
  const locationSortStatus = root.querySelector("[data-location-sort-status]");
  const mapFilterStatus = root.querySelector("[data-map-filter-status]");
  const mapFilterResetButtons = Array.from(root.querySelectorAll("[data-map-filter-reset]"));
  const guideSlug = root.dataset.guideSlug || "";
  const hasMappablePlaces = root.dataset.hasMappablePlaces === "true";
  const locationSortFallbackText =
    root.dataset.locationSortFallbackText || "Location unavailable. Showing curated order instead.";
  const initialParams = new URLSearchParams(window.location.search);
  const LOCATION_SORT_VALUE = "nearby";
  const LOCATION_SORT_FALLBACK = "curated";

  const normalizeTag = (value) => getTagComparisonValue(String(value || ""));
  const allTags = [
    ...new Set(
      JSON.parse(root.dataset.allTags || "[]")
        .map(normalizeTag)
        .filter(Boolean),
    ),
  ];
  const defaultSuggestions = [
    ...new Set(
      JSON.parse(root.dataset.defaultSuggestions || "[]")
        .map(normalizeTag)
        .filter(Boolean),
    ),
  ];
  const typeSeeds = Object.fromEntries(
    Object.entries(JSON.parse(root.dataset.typeSeeds || "{}")).map(([type, tags]) => [
      normalizeTag(type),
      [...new Set((Array.isArray(tags) ? tags : []).map(normalizeTag).filter(Boolean))],
    ]),
  );

  let activeArea = "";
  let activeType = "";
  let activeBudgetSelections = [];
  let activeMinRating = 0;
  let activeMinReviews = 0;
  let selectedTags = [];
  let mapFramePlaceIds = null;
  let searchIndex = null;
  let currentLocation = null;
  let currentLocationStatus = "idle";
  let nearbyDistanceByPlaceId = new Map();
  let hasHandledLocationRequest = false;
  let directLocationRequestInFlight = false;
  let directLocationFallbackTimer = null;
  const highlightedPlaceId = initialParams.get("place") || "";
  let autocompleteTags = [];
  let highlightedAutocompleteIndex = -1;

  const TAG_QUERY_PATTERN = /(?:^|\s)#([a-z0-9-]*)$/i;
  const COMPLETED_TAG_PATTERN = /(^|\s)#([a-z0-9-]+)(?=\s)/gi;

  const escapeHtml = (value) =>
    value
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");

  const getBudgetSelectionKey = (button) =>
    budgetSelectionKey(button.dataset.budgetKind || "", button.dataset.budgetTier || "");

  const budgetKindLabels = Object.fromEntries(
    budgetButtons.map((button) => [
      button.dataset.budgetKind || "",
      button.dataset.budgetKindLabel || button.dataset.budgetKind || "",
    ]),
  );

  const tierLabel = (tier) => "$".repeat(Number(tier) || 0);

  const summarizeBudgetTiers = (tiers) => {
    const values = [...new Set(tiers.map((tier) => Number(tier)).filter(Boolean))].sort(
      (left, right) => left - right,
    );
    if (values.length === 0) {
      return "";
    }
    if (values.length === 1) {
      return tierLabel(values[0]);
    }
    const contiguous = values.every(
      (value, index) => index === 0 || value === values[index - 1] + 1,
    );
    if (contiguous && values[values.length - 1] === 4) {
      return `${tierLabel(values[0])}+`;
    }
    if (contiguous) {
      return `${tierLabel(values[0])}-${tierLabel(values[values.length - 1])}`;
    }
    return values.map(tierLabel).join(",");
  };

  const updateBudgetSummary = () => {
    if (!budgetSummary) {
      return;
    }

    if (activeBudgetSelections.length === 0) {
      budgetSummary.textContent = "Any";
      return;
    }

    const tiersByKind = new Map();
    activeBudgetSelections.forEach((selection) => {
      const [kind, tier] = selection.split(":");
      if (!kind || !tier) {
        return;
      }
      tiersByKind.set(kind, [...(tiersByKind.get(kind) || []), tier]);
    });

    budgetSummary.textContent = [...tiersByKind.entries()]
      .map(([kind, tiers]) =>
        [budgetKindLabels[kind] || kind, summarizeBudgetTiers(tiers)].filter(Boolean).join(" "),
      )
      .join(" · ");
  };

  const getAutocompleteState = (value) => {
    const match = value.match(TAG_QUERY_PATTERN);
    if (!match) {
      return null;
    }

    const hashIndex = value.lastIndexOf("#");
    if (hashIndex < 0) {
      return null;
    }

    return {
      hashIndex,
      fragment: match[1].toLowerCase(),
    };
  };

  const getSanitizedQuery = (value) => {
    const autocompleteState = getAutocompleteState(value);
    const baseValue = autocompleteState ? value.slice(0, autocompleteState.hashIndex) : value;
    return baseValue.trim();
  };

  const consumeCompletedTags = () => {
    if (!searchInput) {
      return;
    }

    const recognizedTags = [];
    const nextValue = searchInput.value
      .replace(COMPLETED_TAG_PATTERN, (match, prefix, tag) => {
        const normalizedTag = normalizeTag(tag);
        if (!normalizedTag || !allTags.includes(normalizedTag)) {
          return match;
        }

        recognizedTags.push(normalizedTag);
        return prefix;
      })
      .replace(/\s{2,}/g, " ")
      .trimStart();

    if (recognizedTags.length === 0) {
      return;
    }

    searchInput.value = nextValue;
    recognizedTags.forEach((tag) => addTag(tag));
  };

  const updateSuggestions = () => {
    if (!suggestionList || !suggestionGroup) {
      return;
    }

    const showSuggestions = !activeType;
    suggestionGroup.hidden = !showSuggestions;
    if (!showSuggestions) {
      suggestionList.innerHTML = "";
      return;
    }

    const suggestions = defaultSuggestions.filter((tag) => !selectedTags.includes(tag));
    suggestionList.innerHTML = suggestions
      .map(
        (tag) => `
          <button class="tag-pill ui-tag-pill" type="button" data-suggestion-tag="${escapeHtml(tag)}">
            <span>#${escapeHtml(tag)}</span>
            <span class="tag-pill-count" data-filter-count data-tag-count-text>0</span>
          </button>
        `,
      )
      .join("");
  };

  const updateSelectedTags = () => {
    if (!selectedTagsRow) {
      return;
    }

    if (selectedTags.length === 0) {
      selectedTagsRow.hidden = true;
      selectedTagsRow.innerHTML = "";
      return;
    }

    selectedTagsRow.hidden = false;
    selectedTagsRow.innerHTML = selectedTags
      .map(
        (tag) => `
          <span class="selected-tag-chip">
            <span class="selected-tag-chip-label">#${escapeHtml(tag)}</span>
            <button class="selected-tag-chip-remove" type="button" data-remove-tag="${escapeHtml(tag)}" aria-label="Remove #${escapeHtml(tag)}">
              ×
            </button>
          </span>
        `,
      )
      .join("");
  };

  const closeAutocomplete = () => {
    autocompleteTags = [];
    highlightedAutocompleteIndex = -1;
    if (autocomplete) {
      autocomplete.hidden = true;
      autocomplete.innerHTML = "";
    }
  };

  const updateAutocomplete = () => {
    if (!autocomplete || !searchInput) {
      return;
    }

    const autocompleteState = getAutocompleteState(searchInput.value);
    if (!autocompleteState) {
      closeAutocomplete();
      return;
    }

    const fragment = autocompleteState.fragment;
    autocompleteTags = allTags
      .filter((tag) => !selectedTags.includes(tag) && (!fragment || tag.startsWith(fragment)))
      .slice(0, 8);

    if (autocompleteTags.length === 0) {
      closeAutocomplete();
      return;
    }

    highlightedAutocompleteIndex = Math.min(
      highlightedAutocompleteIndex < 0 ? 0 : highlightedAutocompleteIndex,
      autocompleteTags.length - 1,
    );

    autocomplete.hidden = false;
    autocomplete.innerHTML = autocompleteTags
      .map((tag, index) => {
        const active = index === highlightedAutocompleteIndex ? ' data-active="true"' : "";
        return `<button class="tag-autocomplete-option" type="button" data-autocomplete-tag="${escapeHtml(tag)}"${active}>#${escapeHtml(tag)}</button>`;
      })
      .join("");
  };

  const addTag = (tag) => {
    const normalizedTag = normalizeTag(tag);
    if (!normalizedTag || selectedTags.includes(normalizedTag)) {
      return;
    }

    selectedTags = [...selectedTags, normalizedTag];
    updateSelectedTags();
    updateSuggestions();
  };

  const removeTag = (tag) => {
    const normalizedTag = normalizeTag(tag);
    selectedTags = selectedTags.filter((candidate) => candidate !== normalizedTag);
    updateSelectedTags();
    updateSuggestions();
  };

  const applyAutocompleteTag = (tag) => {
    if (!searchInput) {
      return;
    }

    const autocompleteState = getAutocompleteState(searchInput.value);
    if (autocompleteState) {
      searchInput.value = searchInput.value
        .slice(0, autocompleteState.hashIndex)
        .replace(/\s+$/, "");
    }

    addTag(tag);
    closeAutocomplete();
    update();
    searchInput.focus();
  };

  const sorters = {
    curated: compareCardsByCurated,
    nearby: (left, right) =>
      compareCardsByNearby(left, right, {
        currentLocation,
        distanceByPlaceId: nearbyDistanceByPlaceId,
      }),
    rating: (left, right) =>
      Number(right.dataset.rating || 0) - Number(left.dataset.rating || 0) ||
      Number(right.dataset.ratingCount || 0) - Number(left.dataset.ratingCount || 0) ||
      sorters.curated(left, right),
    name: (left, right) => (left.dataset.name || "").localeCompare(right.dataset.name || ""),
    neighborhood: compareCardsByNeighborhood,
  };
  const setFilterCount = (button, count) => {
    const countLabel = button.querySelector("[data-filter-count]");
    if (countLabel) {
      countLabel.textContent = String(count);
    }
  };
  const reorderFilterButtons = (buttons, options) => {
    const parent = buttons[0]?.parentElement;
    if (!parent) {
      return;
    }

    sortFilterOptions(options).forEach(({ button }) => {
      parent.appendChild(button);
    });
  };

  const setToggleButtonState = (
    button,
    { active = false, unavailable = false, disabled = false },
  ) => {
    button.dataset.active = active ? "true" : "false";
    button.dataset.unavailable = unavailable ? "true" : "false";
    button.setAttribute("aria-pressed", active ? "true" : "false");
    button.disabled = disabled;
  };

  const compareHighlightedPlace = (left, right) => {
    if (!highlightedPlaceId) {
      return 0;
    }

    const leftIsHighlighted = left.dataset.placeId === highlightedPlaceId;
    const rightIsHighlighted = right.dataset.placeId === highlightedPlaceId;

    if (leftIsHighlighted === rightIsHighlighted) {
      return 0;
    }

    return leftIsHighlighted ? -1 : 1;
  };

  const clearMapFrameFilter = () => {
    mapFramePlaceIds = null;
    update("map-reset");
    root.dispatchEvent(
      new CustomEvent("guide:map-frame-reset", {
        bubbles: true,
      }),
    );
  };

  const setLocationSortMessage = (message = "") => {
    if (!locationSortStatus) {
      return;
    }

    locationSortStatus.hidden = !message;
    locationSortStatus.textContent = message;
  };

  const clearDirectLocationFallbackTimer = () => {
    if (directLocationFallbackTimer !== null) {
      window.clearTimeout(directLocationFallbackTimer);
      directLocationFallbackTimer = null;
    }
  };

  const dispatchUserLocation = ({ coordinates = null, nearGuide, source = "filters", status }) => {
    const detail = {
      coordinates,
      source,
      status,
    };

    if (typeof nearGuide === "boolean") {
      detail.nearGuide = nearGuide;
    }

    root.dispatchEvent(
      new CustomEvent("guide:user-location", {
        bubbles: true,
        detail,
      }),
    );
  };

  const requestCurrentLocationDirectly = () => {
    if (directLocationRequestInFlight) {
      return;
    }

    if (!navigator.geolocation) {
      dispatchUserLocation({ status: "unavailable" });
      return;
    }

    directLocationRequestInFlight = true;
    dispatchUserLocation({ status: "checking" });

    navigator.geolocation.getCurrentPosition(
      (position) => {
        directLocationRequestInFlight = false;
        clearDirectLocationFallbackTimer();
        dispatchUserLocation({
          coordinates: {
            lat: position.coords.latitude,
            lng: position.coords.longitude,
          },
          status: "available",
        });
      },
      (error) => {
        directLocationRequestInFlight = false;
        clearDirectLocationFallbackTimer();
        dispatchUserLocation({
          status: error.code === error.PERMISSION_DENIED ? "denied" : "unavailable",
        });
      },
      {
        maximumAge: 300_000,
        timeout: 10_000,
      },
    );
  };

  const requestCurrentLocation = () => {
    hasHandledLocationRequest = false;
    clearDirectLocationFallbackTimer();
    root.dispatchEvent(
      new CustomEvent("guide:user-location-request", {
        bubbles: true,
      }),
    );

    if (!hasMappablePlaces || !document.querySelector("[data-guide-map]")) {
      requestCurrentLocationDirectly();
      return;
    }

    directLocationFallbackTimer = window.setTimeout(() => {
      if (!hasHandledLocationRequest) {
        requestCurrentLocationDirectly();
      }
    }, 500);
  };

  const refreshNearbyDistances = () => {
    nearbyDistanceByPlaceId = buildNearbyDistanceMap(cards, currentLocation);
  };

  const applySortSelection = (
    sortValue,
    { requestLocationIfNeeded = false, source = "list-filter" } = {},
  ) => {
    if (!sortSelect) {
      return;
    }

    sortSelect.value = sortValue;
    setLocationSortMessage("");

    if (sortValue === LOCATION_SORT_VALUE) {
      if (requestLocationIfNeeded && (!currentLocation || currentLocationStatus === "far")) {
        requestCurrentLocation();
      }

      const nextLocationSortState = resolveLocationSortState({
        fallbackMessage: locationFallbackMessage(currentLocationStatus, locationSortFallbackText),
        fallbackSortValue: LOCATION_SORT_FALLBACK,
        currentLocation,
        currentLocationStatus,
        sortValue,
      });

      if (nextLocationSortState.shouldFallback) {
        sortSelect.value = nextLocationSortState.sortValue;
        setLocationSortMessage(nextLocationSortState.message);
      }
    }

    update(source);
  };

  const update = (source = "list-filter") => {
    const query = searchInput ? getSanitizedQuery(searchInput.value) : "";
    const normalizedQuery = query.toLowerCase();
    const sort = sortSelect?.value || "curated";
    const searchState =
      searchIndex && guideSlug
        ? searchPlaces(query, {
            index: searchIndex,
            scope: "guide",
            guideSlug,
          })
        : null;
    const searchResultIds = searchState
      ? new Set(searchState.results.map((result) => result.entry.id))
      : null;
    const searchScores = searchState
      ? new Map(searchState.results.map((result) => [result.entry.id, result.score]))
      : new Map();
    const activeTypeSeeds = activeType ? typeSeeds[activeType] || [] : [];
    const activeAreaLabel = activeArea
      ? areaButtons.find((button) => (button.dataset.area || "") === activeArea)?.dataset
          .areaLabel || ""
      : "";

    const visibleCards = cards.filter((card) => {
      const visible = cardMatchesFilters(card, {
        activeAreaValue: activeArea,
        activeBudgetSelections,
        activeMinRating,
        activeMinReviews,
        activeTypeValue: activeType,
        activeTypeSeedValues: activeTypeSeeds,
        mapFramePlaceIds,
        normalizedQuery,
        searchResultIds,
        selectedTagValues: selectedTags,
      });
      card.hidden = !visible;
      card.dataset.searchHighlight =
        highlightedPlaceId && card.dataset.placeId === highlightedPlaceId ? "true" : "false";
      return visible;
    });

    const areaCountFilters = {
      activeTypeValue: activeType,
      activeTypeSeedValues: activeTypeSeeds,
      activeBudgetSelections,
      activeMinRating,
      activeMinReviews,
      mapFramePlaceIds,
      normalizedQuery,
      searchResultIds,
      selectedTagValues: selectedTags,
    };
    const typeCountFilters = {
      activeArea,
      activeBudgetSelections,
      activeMinRating,
      activeMinReviews,
      mapFramePlaceIds,
      normalizedQuery,
      searchResultIds,
      selectedTagValues: selectedTags,
    };
    const tagCountFilters = {
      activeArea,
      activeBudgetSelections,
      activeMinRating,
      activeMinReviews,
      activeTypeValue: activeType,
      activeTypeSeedValues: activeTypeSeeds,
      mapFramePlaceIds,
      normalizedQuery,
      searchResultIds,
      selectedTagValues: selectedTags,
    };
    const budgetCountFilters = {
      activeArea,
      activeTypeValue: activeType,
      activeTypeSeedValues: activeTypeSeeds,
      activeMinRating,
      activeMinReviews,
      mapFramePlaceIds,
      normalizedQuery,
      searchResultIds,
      selectedTagValues: selectedTags,
    };
    const broaderAreaCount = activeArea
      ? countAreaOptionCards(cards, areaCountFilters)
      : visibleCards.length;
    const areaMatchCount = activeArea
      ? countAreaOptionCards(cards, areaCountFilters, activeArea)
      : visibleCards.length;
    const areaOverflowCount = activeArea ? Math.max(0, broaderAreaCount - areaMatchCount) : 0;
    const hasAdditionalFilters = hasAdditionalGuideFilters({
      activeTypeValue: activeType,
      activeBudgetSelections,
      activeMinRating,
      activeMinReviews,
      mapFramePlaceIds,
      normalizedQuery,
      selectedTagValues: selectedTags,
      totalCardCount: cards.length,
    });

    const areaOptions = areaButtons.map((button, index) => {
      const areaValue = normalizeTag(button.dataset.area || "");
      const count = countAreaOptionCards(cards, areaCountFilters, areaValue);
      const isActive = areaValue === activeArea;
      const unavailable = Boolean(areaValue) && !isActive && count === 0;
      setFilterCount(button, count);
      setToggleButtonState(button, {
        active: isActive,
        unavailable,
        disabled: unavailable,
      });
      return {
        active: isActive,
        button,
        count,
        originalIndex: index,
        pinned: areaValue === "",
      };
    });
    reorderFilterButtons(areaButtons, areaOptions);

    const typeOptions = typeButtons.map((button, index) => {
      const typeValue = normalizeTag(button.dataset.type || "");
      const count = countTypeOptionCards(cards, typeCountFilters, {
        typeValue,
        typeSeedValues: typeValue ? typeSeeds[typeValue] || [] : [],
      });
      const isActive = typeValue === activeType;
      const unavailable = Boolean(typeValue) && !isActive && count === 0;
      setFilterCount(button, count);
      setToggleButtonState(button, {
        active: isActive,
        unavailable,
        disabled: unavailable,
      });
      return {
        active: isActive,
        button,
        count,
        originalIndex: index,
        pinned: typeValue === "",
      };
    });
    reorderFilterButtons(typeButtons, typeOptions);

    budgetButtons.forEach((button) => {
      const budgetKind = button.dataset.budgetKind || "";
      const budgetTier = button.dataset.budgetTier || "";
      const selectionKey = getBudgetSelectionKey(button);
      const count = countBudgetOptionCards(cards, budgetCountFilters, {
        budgetKind,
        budgetTier,
      });
      const isActive = activeBudgetSelections.includes(selectionKey);
      const unavailable = Boolean(budgetKind && budgetTier) && !isActive && count === 0;
      setFilterCount(button, count);
      setToggleButtonState(button, {
        active: isActive,
        unavailable,
        disabled: unavailable,
      });
    });
    budgetClearButtons.forEach((button) => {
      const kind = button.dataset.budgetClearKind || "";
      const hasKindSelection = activeBudgetSelections.some((selection) =>
        selection.startsWith(`${kind}:`),
      );
      setToggleButtonState(button, {
        active: !hasKindSelection,
        unavailable: false,
        disabled: false,
      });
    });
    updateBudgetSummary();

    const ratingCountFilters = {
      activeArea,
      activeBudgetSelections,
      activeMinReviews,
      activeTypeValue: activeType,
      activeTypeSeedValues: activeTypeSeeds,
      mapFramePlaceIds,
      normalizedQuery,
      searchResultIds,
      selectedTagValues: selectedTags,
    };
    ratingButtons.forEach((button) => {
      const minRating = Number(button.dataset.ratingMin || 0) || 0;
      const count = countRatingOptionCards(cards, ratingCountFilters, minRating);
      const isActive = minRating === activeMinRating;
      const unavailable = minRating > 0 && !isActive && count === 0;
      setFilterCount(button, count);
      setToggleButtonState(button, {
        active: isActive,
        unavailable,
        disabled: unavailable,
      });
    });

    const reviewCountFilters = {
      activeArea,
      activeBudgetSelections,
      activeMinRating,
      activeTypeValue: activeType,
      activeTypeSeedValues: activeTypeSeeds,
      mapFramePlaceIds,
      normalizedQuery,
      searchResultIds,
      selectedTagValues: selectedTags,
    };
    reviewButtons.forEach((button) => {
      const minReviews = Number(button.dataset.reviewMin || 0) || 0;
      const count = countReviewOptionCards(cards, reviewCountFilters, minReviews);
      const isActive = minReviews === activeMinReviews;
      const unavailable = minReviews > 0 && !isActive && count === 0;
      setFilterCount(button, count);
      setToggleButtonState(button, {
        active: isActive,
        unavailable,
        disabled: unavailable,
      });
    });

    if (suggestionList) {
      const suggestionButtons = Array.from(
        suggestionList.querySelectorAll("[data-suggestion-tag]"),
      );
      const suggestionOptions = suggestionButtons.map((button, index) => {
        const suggestionTag = normalizeTag(button.getAttribute("data-suggestion-tag") || "");
        const count = countTagOptionCards(cards, tagCountFilters, suggestionTag);
        const unavailable = count === 0;
        setFilterCount(button, count);
        button.dataset.unavailable = unavailable ? "true" : "false";
        button.disabled = unavailable;
        return {
          active: false,
          button,
          count,
          originalIndex: index,
          pinned: false,
        };
      });
      reorderFilterButtons(suggestionButtons, suggestionOptions);
    }

    const sorter =
      normalizedQuery && sort === "curated" && searchResultIds
        ? (left, right) =>
            (searchScores.get(right.dataset.placeId || "") || 0) -
              (searchScores.get(left.dataset.placeId || "") || 0) || sorters.curated(left, right)
        : sorters[sort] || sorters.curated;

    visibleCards
      .sort((left, right) => compareHighlightedPlace(left, right) || sorter(left, right))
      .forEach((card) => {
        list?.appendChild(card);
      });

    if (resultsCount) {
      const suffix = mapFramePlaceIds ? " in map view" : "";
      resultsCount.textContent = `${visibleCards.length} place${visibleCards.length === 1 ? "" : "s"}${suffix}`;
    }

    if (emptyState) {
      emptyState.dataset.visible = visibleCards.length === 0 ? "true" : "false";
      emptyState.textContent = buildEmptyStateMessage({
        activeAreaLabel,
        overflowCount: areaOverflowCount,
        query,
      });
    }

    if (areaFilterStatus) {
      const message = buildAreaFilterStatusMessage({
        activeAreaLabel,
        hasAdditionalFilters,
        visibleCount: visibleCards.length,
        overflowCount: areaOverflowCount,
      });
      areaFilterStatus.hidden = !message;
      areaFilterStatus.textContent = message;
    }

    if (mapFilterStatus) {
      mapFilterStatus.hidden = !mapFramePlaceIds;
    }

    mapFilterResetButtons.forEach((button) => {
      button.hidden = !mapFramePlaceIds;
    });

    root.dispatchEvent(
      new CustomEvent("guide:places-updated", {
        bubbles: true,
        detail: {
          source,
          mapFrameActive: Boolean(mapFramePlaceIds),
          visiblePlaceIds: visibleCards.map((card) => card.dataset.placeId).filter(Boolean),
        },
      }),
    );
  };

  searchInput?.addEventListener("input", () => {
    consumeCompletedTags();
    updateAutocomplete();
    update();
  });

  searchInput?.addEventListener("keydown", (event) => {
    if (autocompleteTags.length === 0) {
      return;
    }

    if (event.key === "ArrowDown") {
      event.preventDefault();
      highlightedAutocompleteIndex = (highlightedAutocompleteIndex + 1) % autocompleteTags.length;
      updateAutocomplete();
      return;
    }

    if (event.key === "ArrowUp") {
      event.preventDefault();
      highlightedAutocompleteIndex =
        (highlightedAutocompleteIndex - 1 + autocompleteTags.length) % autocompleteTags.length;
      updateAutocomplete();
      return;
    }

    if (event.key === "Enter") {
      event.preventDefault();
      applyAutocompleteTag(autocompleteTags[Math.max(highlightedAutocompleteIndex, 0)]);
      return;
    }

    if (event.key === "Escape") {
      closeAutocomplete();
    }
  });

  searchInput?.addEventListener("blur", () => {
    window.requestAnimationFrame(() => {
      closeAutocomplete();
    });
  });

  sortSelect?.addEventListener("change", () => {
    applySortSelection(sortSelect.value, {
      requestLocationIfNeeded: true,
      source: "sort-change",
    });
  });

  typeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const nextType = normalizeTag(button.dataset.type || "");
      activeType = activeType === nextType ? "" : nextType;
      updateSuggestions();
      update();
    });
  });

  areaButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const nextArea = normalizeTag(button.dataset.area || "");
      activeArea = activeArea === nextArea ? "" : nextArea;
      update();
    });
  });

  budgetButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const selectionKey = getBudgetSelectionKey(button);
      if (!selectionKey) {
        return;
      }
      activeBudgetSelections = toggleBudgetSelection(activeBudgetSelections, selectionKey);
      update();
    });
  });

  budgetClearButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const kind = button.dataset.budgetClearKind || "";
      activeBudgetSelections = activeBudgetSelections.filter(
        (selection) => !selection.startsWith(`${kind}:`),
      );
      update();
    });
  });

  ratingButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const nextMinRating = Number(button.dataset.ratingMin || 0) || 0;
      activeMinRating = activeMinRating === nextMinRating ? 0 : nextMinRating;
      update();
    });
  });

  reviewButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const nextMinReviews = Number(button.dataset.reviewMin || 0) || 0;
      activeMinReviews = activeMinReviews === nextMinReviews ? 0 : nextMinReviews;
      update();
    });
  });

  suggestionList?.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) {
      return;
    }

    const button = event.target.closest("[data-suggestion-tag]");
    if (!(button instanceof HTMLElement)) {
      return;
    }

    const tag = button.dataset.suggestionTag || "";
    if (!tag) {
      return;
    }

    if (selectedTags.includes(tag)) {
      removeTag(tag);
    } else {
      addTag(tag);
    }

    update();
    searchInput?.focus();
  });

  selectedTagsRow?.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) {
      return;
    }

    const button = event.target.closest("[data-remove-tag]");
    if (!(button instanceof HTMLElement)) {
      return;
    }

    const tag = button.dataset.removeTag || "";
    removeTag(tag);
    update();
    searchInput?.focus();
  });

  autocomplete?.addEventListener("mousedown", (event) => {
    event.preventDefault();
  });

  autocomplete?.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) {
      return;
    }

    const button = event.target.closest("[data-autocomplete-tag]");
    if (!(button instanceof HTMLElement)) {
      return;
    }

    const tag = button.dataset.autocompleteTag || "";
    applyAutocompleteTag(tag);
  });

  root.addEventListener("guide:map-frame-filter", (event) => {
    const visiblePlaceIds = Array.isArray(event.detail?.visiblePlaceIds)
      ? event.detail.visiblePlaceIds
      : [];
    mapFramePlaceIds = new Set(visiblePlaceIds);
    update("map-frame");
  });

  root.addEventListener("guide:map-frame-reset-request", clearMapFrameFilter);

  root.addEventListener("guide:sort-request", (event) => {
    const sortValue = event.detail?.sortValue || "";
    if (sortValue !== LOCATION_SORT_VALUE) {
      return;
    }

    applySortSelection(sortValue, {
      source: "map-sort-request",
    });
  });
  root.addEventListener("guide:user-location", (event) => {
    hasHandledLocationRequest = true;
    clearDirectLocationFallbackTimer();
    const detail = event.detail || {};
    currentLocationStatus = detail.status || "idle";
    currentLocation = normalizeUserLocationDetail(detail);
    refreshNearbyDistances();

    if (currentLocation && currentLocationStatus !== "far") {
      setLocationSortMessage("");
      if (sortSelect?.value === LOCATION_SORT_VALUE) {
        update("location-sort");
      }
      return;
    }

    const nextLocationSortState = resolveLocationSortState({
      fallbackMessage: locationFallbackMessage(currentLocationStatus, locationSortFallbackText),
      fallbackSortValue: LOCATION_SORT_FALLBACK,
      currentLocation,
      currentLocationStatus,
      sortValue: sortSelect?.value,
    });

    if (nextLocationSortState.shouldFallback && sortSelect) {
      sortSelect.value = nextLocationSortState.sortValue;
      setLocationSortMessage(nextLocationSortState.message);
      update("location-sort-fallback");
    }
  });

  mapFilterResetButtons.forEach((button) => {
    button.addEventListener("click", clearMapFrameFilter);
  });

  const applyDeepLink = () => {
    const initialQuery = initialParams.get("q") || "";
    if (initialQuery && searchInput) {
      searchInput.value = initialQuery;
    }

    getInitialSelectedTags({
      allTags,
      params: initialParams,
    }).forEach((tag) => addTag(tag));
  };

  const scrollToHighlightedPlace = () => {
    if (!highlightedPlaceId) {
      return;
    }
    const target = cards.find((card) => card.dataset.placeId === highlightedPlaceId);
    if (!target) {
      return;
    }
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.focus({ preventScroll: true });
  };

  updateSelectedTags();
  updateSuggestions();
  applyDeepLink();
  consumeCompletedTags();
  updateAutocomplete();
  update();

  loadSearchIndex()
    .then((index) => {
      searchIndex = index;
      update();
      requestAnimationFrame(scrollToHighlightedPlace);
    })
    .catch(() => {
      update();
      requestAnimationFrame(scrollToHighlightedPlace);
    });
}
