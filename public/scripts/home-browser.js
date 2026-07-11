import { nearbyGuidesForLocation } from "./home-browser-nearby.js";
import { parseHomeBrowserHash, serializeHomeBrowserHash } from "./home-browser-state.js";
import { loadSearchIndex, searchGuides, searchPlaces } from "./place-search.js";
import { buildSearchConversation, buildSearchSuggestions } from "./search-conversation.js";

const GROUP_LIMIT = 5;
const INDIVIDUAL_RESULT_LIMIT = 24;
const FLAG_SUFFIX_PATTERN = /(?:\s*[\u{1F1E6}-\u{1F1FF}]{2})+$/u;

const root = document.querySelector("[data-home-browser-root]");

if (root) {
  const countrySectionsVisible = root.dataset.countrySectionsVisible !== "false";
  const searchInput = root.querySelector("[data-home-search-input]");
  const countryBlocks = Array.from(root.querySelectorAll("[data-country-block]"));
  const countryButtons = Array.from(root.querySelectorAll("[data-country-filter]"));
  const resultsCount = root.querySelector("[data-home-results-count]");
  const emptyState = root.querySelector("[data-home-empty-state]");
  const locationButton = root.querySelector("[data-location-target]");
  const locationStatus = root.querySelector("[data-location-status]");
  const homeGuideMap = root.querySelector("[data-home-guide-map]");
  const globalResults = root.querySelector("[data-global-search-results]");
  const globalResultsTitle = root.querySelector("[data-global-search-title]");
  const globalResultsConversation = root.querySelector("[data-global-search-conversation]");
  const globalResultsSummary = root.querySelector("[data-global-search-summary]");
  const globalResultsSuggestions = root.querySelector("[data-global-search-suggestions]");
  const globalResultsToolbar = root.querySelector("[data-global-search-toolbar]");
  const globalResultsList = root.querySelector("[data-global-search-list]");
  const groupedResultsList = root.querySelector("[data-grouped-search-list]");
  const globalResultsEmpty = root.querySelector("[data-global-search-empty]");
  const searchViewToggles = Array.from(root.querySelectorAll("[data-search-view-toggle]"));
  const validCountries = countryButtons
    .map((button) => button.dataset.country || "")
    .filter((country) => Boolean(country));

  let activeCountry = "";
  let locationMatchCountry = "";
  let nearbyGuideState = null;
  let searchIndex = null;
  let searchIndexUnavailable = false;
  let searchResultView = "grouped";

  const pluralize = (count, singular, plural = `${singular}s`) =>
    `${count} ${count === 1 ? singular : plural}`;

  const normalizeCountry = (value) =>
    String(value || "")
      .trim()
      .toLowerCase();

  const countryLabel = (country) => {
    if (!country) {
      return "";
    }

    const button = countryButtons.find(
      (candidate) => (candidate.dataset.country || "") === country,
    );
    const label = button?.querySelector("span")?.textContent || country;
    return label.replace(FLAG_SUFFIX_PATTERN, "").trim();
  };

  const syncUrlState = () => {
    const nextHash = serializeHomeBrowserHash({
      country: activeCountry,
      query: searchInput?.value || "",
      view: searchResultView,
    });
    if (window.location.hash === nextHash) {
      return;
    }

    history.replaceState(
      history.state,
      "",
      `${window.location.pathname}${window.location.search}${nextHash}`,
    );
  };

  const applyUrlState = () => {
    const state = parseHomeBrowserHash(window.location.hash, validCountries);

    activeCountry = state.country;
    nearbyGuideState = null;
    searchResultView = state.view;
    locationMatchCountry = "";
    setLocationButtonActive(false);
    setLocationStatus("");

    if (searchInput) {
      searchInput.value = state.query;
    }
  };

  const guideLocations = (() => {
    try {
      return JSON.parse(homeGuideMap?.dataset.guides || "[]").filter(
        (guide) =>
          guide &&
          typeof guide.country === "string" &&
          typeof guide.countryName === "string" &&
          typeof guide.lat === "number" &&
          typeof guide.lng === "number",
      );
    } catch {
      return [];
    }
  })();

  const setLocationStatus = (message, tone = "") => {
    if (!locationStatus) {
      return;
    }

    locationStatus.textContent = message;
    locationStatus.dataset.tone = tone;
  };

  const setLocationButtonBusy = (busy) => {
    if (!locationButton) {
      return;
    }

    locationButton.disabled = busy;
    locationButton.dataset.busy = busy ? "true" : "false";
    locationButton.setAttribute("aria-busy", busy ? "true" : "false");
  };

  const setLocationButtonActive = (active) => {
    if (!locationButton) {
      return;
    }

    locationButton.dataset.active = active ? "true" : "false";
    const label = active ? "Clear nearby guides" : "Show nearby guides";
    locationButton.title = label;
    locationButton.setAttribute("aria-label", label);
  };

  const formatDistanceKm = (distanceKm) => {
    if (!Number.isFinite(distanceKm)) {
      return "";
    }
    if (distanceKm >= 100) {
      return `${Math.round(distanceKm / 10) * 10} km`;
    }
    if (distanceKm >= 10) {
      return `${Math.round(distanceKm)} km`;
    }
    return `${distanceKm.toFixed(1)} km`;
  };

  const createGuideLink = (entry, query) => {
    const params = new URLSearchParams();
    params.set("place", entry.id);
    if (query) {
      params.set("q", query);
    }
    return `/guides/${entry.guide_slug}/?${params.toString()}`;
  };

  const truncateText = (value, maxLength) => {
    if (!value || value.length <= maxLength) {
      return value || "";
    }
    return `${value.slice(0, maxLength - 1).trim()}...`;
  };

  const ratingValue = (value) =>
    typeof value === "number" && Number.isFinite(value) ? value : null;
  const reviewCountValue = (value) =>
    typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  const formatRating = (value) => value.toFixed(1);
  const reviewCountFormatter = new Intl.NumberFormat("en-US");
  const compactReviewCountFormatter = new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  });
  const formatReviewCount = (value) =>
    (value >= 1000 ? compactReviewCountFormatter : reviewCountFormatter).format(value);
  const formatReviewLabel = (value) =>
    `${formatReviewCount(value)} ${value === 1 ? "review" : "reviews"}`;

  const groupSearchResults = (results) =>
    [
      ...results
        .reduce((groups, result) => {
          const key = result.entry.guide_slug || result.entry.guide_title || "unknown-guide";
          if (!groups.has(key)) {
            groups.set(key, []);
          }
          groups.get(key).push(result);
          return groups;
        }, new Map())
        .entries(),
    ]
      .map(([key, items]) => {
        const entry = items[0]?.entry || {};
        return {
          key,
          guideSlug: entry.guide_slug || "",
          guideTitle: entry.guide_title || "Guide",
          city: entry.city || "",
          country: entry.country || "",
          items,
        };
      })
      .sort(
        (left, right) =>
          right.items.length - left.items.length ||
          right.items[0].score - left.items[0].score ||
          left.guideTitle.localeCompare(right.guideTitle),
      );

  const updateSearchViewButtons = () => {
    searchViewToggles.forEach((toggle) => {
      const selected = (toggle.dataset.searchView || "") === searchResultView;
      toggle.dataset.active = selected ? "true" : "false";
      toggle.setAttribute("aria-pressed", selected ? "true" : "false");
    });
  };

  const createSearchResultCard = (result, query) => {
    const entry = result.entry;
    const card = document.createElement("article");
    card.className = "search-result-card";

    const title = document.createElement("h4");
    title.textContent = entry.name || "Saved place";

    const meta = document.createElement("p");
    meta.className = "meta-copy";
    meta.textContent = [
      entry.guide_title,
      [entry.category, entry.neighborhood].filter(Boolean).join(" · "),
      [entry.city, entry.country].filter(Boolean).join(", "),
    ]
      .filter(Boolean)
      .join(" · ");

    const rating = ratingValue(entry.rating);
    const reviewCount = reviewCountValue(entry.user_rating_count);
    const stats = document.createElement("div");
    stats.className = "stats-row";
    if (rating !== null) {
      const ratingPill = document.createElement("span");
      ratingPill.className = "stat-pill";
      ratingPill.textContent = `${formatRating(rating)} ★`;
      stats.append(ratingPill);
    }
    if (reviewCount !== null) {
      const reviewPill = document.createElement("span");
      reviewPill.className = "stat-pill";
      reviewPill.textContent = formatReviewLabel(reviewCount);
      stats.append(reviewPill);
    }
    stats.hidden = stats.childElementCount === 0;

    const copy = document.createElement("p");
    copy.className = "small-copy";
    copy.textContent = truncateText(entry.why_recommended || entry.note || "", 180);
    copy.hidden = !copy.textContent;

    const tags = document.createElement("div");
    tags.className = "tag-row";
    [...(entry.vibe_tags || []), ...(entry.tags || [])].slice(0, 5).forEach((tag) => {
      const pill = document.createElement("span");
      pill.className = "tag-pill";
      pill.textContent = tag.includes("-") ? tag.replaceAll("-", " ") : `#${tag}`;
      tags.appendChild(pill);
    });
    tags.hidden = tags.childElementCount === 0;

    const link = document.createElement("a");
    link.className = "action-pill";
    link.href = createGuideLink(entry, query);
    link.textContent = "Open in guide";

    card.append(title, meta, stats, copy, tags, link);
    return card;
  };

  const createGroupedGuideCard = (group, query) => {
    const card = document.createElement("article");
    card.className = "search-country-card";

    const header = document.createElement("div");
    header.className = "search-country-card-head";

    const titleBlock = document.createElement("div");
    titleBlock.className = "search-country-card-title";

    const title = document.createElement("h4");
    title.textContent = group.guideTitle;

    const meta = document.createElement("p");
    meta.className = "search-country-item-meta";
    meta.textContent = [group.city, group.country].filter(Boolean).join(", ");
    meta.hidden = !meta.textContent;

    titleBlock.append(title, meta);

    const count = document.createElement("p");
    count.className = "small-copy";
    count.textContent =
      group.items.length > GROUP_LIMIT
        ? `${pluralize(group.items.length, "match", "matches")} · top ${GROUP_LIMIT} shown`
        : pluralize(group.items.length, "match", "matches");

    header.append(titleBlock, count);

    const list = document.createElement("div");
    list.className = "search-country-list";

    group.items.slice(0, GROUP_LIMIT).forEach((result) => {
      const entry = result.entry;
      const link = document.createElement("a");
      link.className = "search-country-item";
      link.href = createGuideLink(entry, query);

      const itemTitle = document.createElement("span");
      itemTitle.className = "search-country-item-title";
      itemTitle.textContent = entry.name || "Saved place";

      const itemMeta = document.createElement("span");
      itemMeta.className = "search-country-item-meta";
      itemMeta.textContent = [entry.category, entry.neighborhood].filter(Boolean).join(" · ");
      itemMeta.hidden = !itemMeta.textContent;

      const rating = ratingValue(entry.rating);
      const reviewCount = reviewCountValue(entry.user_rating_count);
      const itemStats = document.createElement("span");
      itemStats.className = "search-country-item-meta";
      itemStats.textContent = [
        rating !== null ? `${formatRating(rating)} ★` : null,
        reviewCount !== null ? formatReviewLabel(reviewCount) : null,
      ]
        .filter(Boolean)
        .join(" · ");
      itemStats.hidden = !itemStats.textContent;

      link.append(itemTitle, itemMeta, itemStats);
      list.append(link);
    });

    card.append(header, list);

    if (group.items.length > GROUP_LIMIT) {
      const overflow = document.createElement("p");
      overflow.className = "search-country-overflow";
      overflow.textContent = `${group.items.length - GROUP_LIMIT} more matching place${
        group.items.length - GROUP_LIMIT === 1 ? "" : "s"
      }. Switch to individual view to browse more.`;
      card.append(overflow);
    }

    return card;
  };

  const dispatchMapUpdate = (visibleGuideSlugs, visibleGuideCount, query) => {
    const activeScopeMode = nearbyGuideState ? "nearby" : activeCountry ? "country" : "";
    const activeScopeLabel = nearbyGuideState ? "near you" : countryLabel(activeCountry);
    document.dispatchEvent(
      new CustomEvent("favorite-places:home-map-update", {
        detail: {
          activeScopeLabel,
          activeScopeMode,
          query,
          visibleGuideCount,
          visibleGuideSlugs,
        },
      }),
    );
  };

  const renderSearchConversation = (query, state, visibleResults) => {
    if (!globalResultsConversation || !globalResultsSummary || !globalResultsSuggestions) {
      return;
    }

    if (!query || !state) {
      globalResultsConversation.hidden = true;
      globalResultsSummary.textContent = "";
      globalResultsSuggestions.replaceChildren();
      return;
    }

    const scopeLabel = activeCountry
      ? countryLabel(activeCountry)
      : nearbyGuideState
        ? "nearby guides"
        : "your guides";
    globalResultsConversation.hidden = false;
    globalResultsSummary.textContent = buildSearchConversation({
      count: visibleResults.length,
      parsed: state.parsed,
      scopeLabel,
    });
    globalResultsSuggestions.replaceChildren(
      ...buildSearchSuggestions({ parsed: state.parsed, results: visibleResults }).map(
        (suggestion) => {
          const button = document.createElement("button");
          button.className = "tag-pill ui-tag-pill";
          button.type = "button";
          button.dataset.globalSearchSuggestion = suggestion.query;
          button.textContent = suggestion.label;
          return button;
        },
      ),
    );
  };

  const renderGlobalSearch = (query, placeSearchState, filteredResults) => {
    if (!globalResults || !globalResultsList || !groupedResultsList || !globalResultsEmpty) {
      return;
    }

    globalResults.hidden = !query;
    globalResults.setAttribute("aria-hidden", query ? "false" : "true");

    if (!query) {
      globalResultsList.replaceChildren();
      groupedResultsList.replaceChildren();
      globalResultsList.hidden = true;
      groupedResultsList.hidden = false;
      globalResultsEmpty.dataset.visible = "false";
      if (globalResultsToolbar) {
        globalResultsToolbar.hidden = false;
      }
      if (globalResultsTitle) {
        globalResultsTitle.textContent = "Matching places";
      }
      if (globalResultsSummary) {
        globalResultsSummary.textContent = "";
      }
      renderSearchConversation("", null, []);
      updateSearchViewButtons();
      return;
    }

    if (searchIndexUnavailable) {
      globalResultsList.replaceChildren();
      groupedResultsList.replaceChildren();
      globalResultsList.hidden = true;
      groupedResultsList.hidden = true;
      globalResultsEmpty.dataset.visible = "true";
      globalResultsEmpty.textContent = "Place search is unavailable. Guide browsing still works.";
      if (globalResultsToolbar) {
        globalResultsToolbar.hidden = true;
      }
      if (globalResultsTitle) {
        globalResultsTitle.textContent = "Place search unavailable";
      }
      if (globalResultsSummary) {
        globalResultsSummary.textContent = "";
      }
      renderSearchConversation("", null, []);
      return;
    }

    if (!searchIndex) {
      globalResultsList.replaceChildren();
      groupedResultsList.replaceChildren();
      globalResultsEmpty.dataset.visible = "false";
      if (globalResultsToolbar) {
        globalResultsToolbar.hidden = true;
      }
      if (globalResultsTitle) {
        globalResultsTitle.textContent = "Loading matching places";
      }
      if (globalResultsSummary) {
        globalResultsSummary.textContent = "Loading index...";
      }
      renderSearchConversation("", null, []);
      return;
    }

    const state = placeSearchState || searchPlaces(query, { index: searchIndex, scope: "all" });
    const visibleResults =
      filteredResults ||
      state.results.filter(
        (result) =>
          (!activeCountry || normalizeCountry(result.entry.country) === activeCountry) &&
          (!nearbyGuideState || nearbyGuideState.guideSlugs.has(result.entry.guide_slug)),
      );
    const groupedResults = groupSearchResults(visibleResults);
    const visibleIndividualResults = visibleResults.slice(0, INDIVIDUAL_RESULT_LIMIT);

    globalResultsList.replaceChildren(
      ...visibleIndividualResults.map((result) => createSearchResultCard(result, query)),
    );
    groupedResultsList.replaceChildren(
      ...groupedResults.map((group) => createGroupedGuideCard(group, query)),
    );

    globalResultsList.hidden = searchResultView !== "individual";
    groupedResultsList.hidden = searchResultView !== "grouped";
    globalResultsEmpty.dataset.visible = visibleResults.length === 0 ? "true" : "false";
    globalResultsEmpty.textContent = activeCountry
      ? `No matching places in ${countryLabel(activeCountry)}. Try a broader search.`
      : nearbyGuideState
        ? "No matching places in nearby guides. Try a broader search."
        : "No matching places. Try a broader search.";

    if (globalResultsToolbar) {
      globalResultsToolbar.hidden = false;
    }

    if (globalResultsTitle) {
      globalResultsTitle.textContent =
        searchResultView === "grouped"
          ? `${pluralize(visibleResults.length, "matching place")} across ${pluralize(
              groupedResults.length,
              "guide",
            )}`
          : `${pluralize(visibleResults.length, "matching place")}`;
    }

    renderSearchConversation(query, state, visibleResults);

    updateSearchViewButtons();
  };

  const updateCountryButtons = (matchingGuidesByCountry, totalMatchingGuides, searching) => {
    countryButtons.forEach((button) => {
      const country = button.dataset.country || "";
      const count = country ? matchingGuidesByCountry.get(country) || 0 : totalMatchingGuides;
      const countLabel = button.querySelector(".country-filter-count");
      const shouldHide =
        (searching || Boolean(nearbyGuideState)) &&
        Boolean(country) &&
        country !== activeCountry &&
        count === 0;

      button.dataset.active = country === activeCountry ? "true" : "false";
      button.dataset.locationMatch = country && country === locationMatchCountry ? "true" : "false";
      button.setAttribute("aria-pressed", country === activeCountry ? "true" : "false");
      button.disabled = Boolean(country) && country !== activeCountry && count === 0;
      button.hidden = shouldHide;

      if (countLabel) {
        countLabel.textContent = String(count);
      }
    });
  };

  const update = () => {
    const rawQuery = (searchInput?.value || "").trim();
    const normalizedQuery = rawQuery.toLowerCase();
    const nearbyGuideSlugs = nearbyGuideState?.guideSlugs || null;
    const placeSearchState =
      searchIndex && rawQuery ? searchPlaces(rawQuery, { index: searchIndex, scope: "all" }) : null;
    const filteredPlaceResults = placeSearchState
      ? placeSearchState.results.filter(
          (result) =>
            (!activeCountry || normalizeCountry(result.entry.country) === activeCountry) &&
            (!nearbyGuideSlugs || nearbyGuideSlugs.has(result.entry.guide_slug)),
        )
      : [];
    const placeMatchGuideSlugs = placeSearchState
      ? new Set(filteredPlaceResults.map((result) => result.entry.guide_slug))
      : null;
    const guideMatches =
      searchIndex && rawQuery
        ? new Set(searchGuides(rawQuery, { index: searchIndex }).map((result) => result.guide.slug))
        : null;
    const matchingGuidesByCountry = new Map();
    const visibleGuideSlugs = [];
    let visibleGuideCount = 0;
    let visibleCountryCount = 0;
    let totalMatchingGuides = 0;

    countryBlocks.forEach((block) => {
      const country = block.dataset.country || "";
      const cards = Array.from(block.querySelectorAll("[data-guide-card]"));
      const matchingCards = cards.filter((card) => {
        const guideSlug = card.dataset.guideSlug || "";
        if (nearbyGuideSlugs && !nearbyGuideSlugs.has(guideSlug)) {
          return false;
        }
        if (!normalizedQuery) {
          return true;
        }
        return (
          guideMatches?.has(guideSlug) ||
          placeMatchGuideSlugs?.has(guideSlug) ||
          (!searchIndex && (card.dataset.search || "").includes(normalizedQuery))
        );
      });
      const matchingCardSet = new Set(matchingCards);

      block.dataset.locationMatch = country && country === locationMatchCountry ? "true" : "false";
      matchingGuidesByCountry.set(country, matchingCards.length);
      totalMatchingGuides += matchingCards.length;

      const selectedCountryMatches = !activeCountry || country === activeCountry;
      let blockVisibleGuideCount = 0;

      cards.forEach((card) => {
        const visible = selectedCountryMatches && matchingCardSet.has(card);
        card.hidden = !visible;

        if (visible) {
          blockVisibleGuideCount += 1;
          if (card.dataset.guideSlug) {
            visibleGuideSlugs.push(card.dataset.guideSlug);
          }
        }
      });

      block.hidden = !countrySectionsVisible || blockVisibleGuideCount === 0;

      if (blockVisibleGuideCount > 0) {
        visibleCountryCount += 1;
        visibleGuideCount += blockVisibleGuideCount;
      }
    });

    updateCountryButtons(matchingGuidesByCountry, totalMatchingGuides, Boolean(rawQuery));

    if (resultsCount) {
      resultsCount.textContent = nearbyGuideState
        ? `${pluralize(visibleGuideCount, "nearby guide")} across ${pluralize(
            visibleCountryCount,
            "country",
            "countries",
          )}`
        : `${pluralize(visibleGuideCount, "guide")} across ${pluralize(
            visibleCountryCount,
            "country",
            "countries",
          )}`;
    }

    if (emptyState) {
      emptyState.dataset.visible = visibleGuideCount === 0 ? "true" : "false";
      emptyState.textContent = nearbyGuideState
        ? "No nearby guides match right now. Clear the nearby filter or choose a country."
        : "No matching guides. Try a broader search or choose all countries.";
    }

    dispatchMapUpdate([...new Set(visibleGuideSlugs)], visibleGuideCount, rawQuery);
    renderGlobalSearch(rawQuery, placeSearchState, filteredPlaceResults);
    syncUrlState();
  };

  const selectCountry = (country, { scroll = false } = {}) => {
    activeCountry = country;
    nearbyGuideState = null;
    locationMatchCountry = "";
    setLocationButtonActive(false);
    setLocationStatus("");

    update();

    if (scroll) {
      const button = countryButtons.find(
        (candidate) => (candidate.dataset.country || "") === country,
      );
      const block = countryBlocks.find(
        (candidate) => (candidate.dataset.country || "") === country,
      );

      button?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
      if (countrySectionsVisible) {
        block?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
  };

  const selectNearbyGuides = (state, { scroll = false } = {}) => {
    nearbyGuideState = state;
    activeCountry = "";
    locationMatchCountry = state.nearestGuide?.country || "";
    setLocationButtonActive(true);

    if (searchInput) {
      searchInput.value = "";
    }

    update();

    if (scroll) {
      const firstNearbyCountry = state.guides[0]?.country;
      const block = countryBlocks.find(
        (candidate) => (candidate.dataset.country || "") === firstNearbyCountry,
      );
      locationButton?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
      if (countrySectionsVisible) {
        block?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
  };

  const setSearchView = (view) => {
    const nextView = view === "individual" ? "individual" : "grouped";
    if (searchResultView === nextView) {
      renderGlobalSearch((searchInput?.value || "").trim());
      return;
    }

    searchResultView = nextView;
    renderGlobalSearch((searchInput?.value || "").trim());
  };

  searchInput?.addEventListener("input", update);

  countryButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const country = button.dataset.country || "";
      selectCountry(country === activeCountry ? "" : country);
    });
  });

  searchViewToggles.forEach((toggle) => {
    toggle.addEventListener("click", () => {
      setSearchView(toggle.dataset.searchView || "grouped");
    });
  });

  globalResultsSuggestions?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-global-search-suggestion]");
    const suggestion = button?.dataset.globalSearchSuggestion || "";
    if (!suggestion || !searchInput) return;

    searchInput.value = `${searchInput.value.trim()} ${suggestion}`.trim();
    searchInput.focus();
    update();
  });

  window.addEventListener("hashchange", () => {
    applyUrlState();
    updateSearchViewButtons();
    update();
  });

  if (locationButton) {
    if (!("geolocation" in navigator) || guideLocations.length === 0) {
      locationButton.disabled = true;
      setLocationStatus("Location is not available.", "error");
    } else {
      setLocationButtonActive(false);
      locationButton.addEventListener("click", () => {
        if (nearbyGuideState) {
          nearbyGuideState = null;
          locationMatchCountry = "";
          setLocationButtonActive(false);
          setLocationStatus("Nearby filter cleared.");
          update();
          return;
        }

        setLocationButtonBusy(true);
        setLocationStatus("Locating...", "pending");

        navigator.geolocation.getCurrentPosition(
          (position) => {
            const nearbyGuides = nearbyGuidesForLocation(
              guideLocations,
              position.coords.latitude,
              position.coords.longitude,
            );

            setLocationButtonBusy(false);

            if (!nearbyGuides) {
              setLocationStatus("No nearby guides have map data.", "error");
              return;
            }

            if (!nearbyGuides.isNearMatch) {
              nearbyGuideState = null;
              locationMatchCountry = "";
              setLocationButtonActive(false);
              setLocationStatus(
                `No guides are close to you yet. The nearest guide is ${formatDistanceKm(
                  nearbyGuides.nearestGuide.distance,
                )} away.`,
                "error",
              );
              update();
              return;
            }

            selectNearbyGuides(nearbyGuides, { scroll: true });
            setLocationStatus(
              `Showing ${pluralize(nearbyGuides.guides.length, "guide")} within ${formatDistanceKm(
                nearbyGuides.radiusKm,
              )} of you.`,
              "success",
            );
          },
          (error) => {
            setLocationButtonBusy(false);

            if (error.code === error.PERMISSION_DENIED) {
              setLocationStatus("Location permission was not granted.", "error");
              return;
            }

            if (error.code === error.TIMEOUT) {
              setLocationStatus("Location timed out. Try again.", "error");
              return;
            }

            setLocationStatus("Could not get your location.", "error");
          },
          {
            enableHighAccuracy: false,
            maximumAge: 10 * 60 * 1000,
            timeout: 10000,
          },
        );
      });
    }
  }

  applyUrlState();
  updateSearchViewButtons();
  update();

  loadSearchIndex()
    .then((index) => {
      searchIndex = index;
      update();
    })
    .catch(() => {
      searchIndexUnavailable = true;
      update();
    });
}
