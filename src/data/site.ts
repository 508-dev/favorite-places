import { siteConfig as configuredSiteConfig } from "favorite-places:site-config";

interface GuideCardConfig {
  showDescription: boolean;
  fallbackDescription: string;
  showAuthor: boolean;
  authorLabel: string;
  showTags: boolean;
  maxTags: number;
  showPlaceCount: boolean;
  trimCountryFromTitle: boolean;
}

interface HomeConfig {
  eyebrow: string;
  title: string;
  intro: string;
  highlightsHeading: string;
  highlights: string[];
  guidesEyebrow: string;
  guidesHeading: string;
  guidesLinkText: string | null;
  searchPlaceholder: string;
  showHero: boolean;
  showHeroStats: boolean;
  showGuideBrowser: boolean;
  showGuideBrowserControls: boolean;
  showMap: boolean;
  showCountrySections: boolean;
  searchLabel: string;
  showingLabel: string;
  nearMeLabel: string;
  countriesLabel: string;
  allCountriesLabel: string;
  emptyStateText: string;
  searchResultsEyebrow: string;
  searchResultsHeading: string;
  searchResultsGroupedLabel: string;
  searchResultsIndividualLabel: string;
  searchResultsEmptyText: string;
  guideCard: GuideCardConfig;
}

interface GuideConfig {
  fallbackDescription: string;
  showAuthor: boolean;
  authorLabel: string;
  sidebarContent: string[];
  backLinkLabel: string;
  sourceListLabel: string;
  topPicksEyebrow: string;
  topPicksHeading: string;
  bestHitsEyebrow: string;
  bestHitsHeading: string;
  browseEyebrow: string;
  placesHeading: string;
  showTopPicks: boolean;
  showBestHits: boolean;
  showGuideTags: boolean;
  listTagsHeading: string;
  searchLabel: string;
  searchHint: string;
  searchPlaceholder: string;
  areaLabel: string;
  broaderAreaLabel: string;
  typeLabel: string;
  popularTagsLabel: string;
  popularTagsHint: string;
  allFilterLabel: string;
  resultsSortLabel: string;
  sortCuratedLabel: string;
  sortNearMeLabel: string;
  sortRatingLabel: string;
  sortNameLabel: string;
  sortNeighborhoodLabel: string;
  locationSortFallbackText: string;
  resetMapLabel: string;
  mapFilteredText: string;
  emptyStateText: string;
}

interface PlaceCardConfig {
  showPhoto: boolean;
  showMapLink: boolean;
  showAttribution: boolean;
  showGuideAuthorAttribution: boolean;
  showCategory: boolean;
  showNeighborhood: boolean;
  showRating: boolean;
  showReviewCount: boolean;
  showPriceRange: boolean;
  showTrustSignals: boolean;
  showTopPickBadge: boolean;
  showWhyRecommended: boolean;
  showAddress: boolean;
  showTags: boolean;
  maxTags: number;
  showStatus: boolean;
  bestHitLabel: string;
  topPickLabel: string;
  attributionLabel: string;
  mapsLabel: string;
  savedPlaceLabel: string;
  photoPlaceholderFallback: string;
}

export interface SiteConfig {
  siteName: string;
  ownerName: string;
  siteDescription: string;
  defaultSeoImage: string | null;
  favicon: {
    href: string;
    type?: string;
    sizes?: string;
  } | null;
  logo: {
    href: string;
    src: string;
    alt: string;
  } | null;
  navLinks: {
    label: string;
    href: string;
  }[];
  mapProvider: "auto" | "google" | "leaflet";
  home: HomeConfig;
  guide: GuideConfig;
  placeCard: PlaceCardConfig;
}

export type SiteConfigInput = Partial<
  Omit<SiteConfig, "favicon" | "logo" | "navLinks" | "home" | "guide" | "placeCard">
> & {
  favicon?: SiteConfig["favicon"];
  logo?: SiteConfig["logo"];
  navLinks?: SiteConfig["navLinks"];
  guideCard?: Partial<GuideCardConfig>;
  home?: Partial<Omit<HomeConfig, "guideCard">> & {
    guideCard?: Partial<GuideCardConfig>;
  };
  guide?: Partial<GuideConfig>;
  placeCard?: Partial<PlaceCardConfig>;
};

const defaultSiteConfig: SiteConfig = {
  siteName: "Favorite Places",
  ownerName: "Your Name",
  siteDescription: "Saved places organized into shareable city guides.",
  defaultSeoImage: null,
  favicon: null,
  logo: null,
  navLinks: [{ label: "Places", href: "/" }],
  mapProvider: "auto",
  home: {
    eyebrow: "Favorite Places",
    title: "Trip suggestions",
    intro: "Saved restaurants, cafes, bars, shops, and sights by city.",
    highlightsHeading: "About these guides",
    highlights: [],
    guidesEyebrow: "Guides",
    guidesHeading: "Browse by country",
    guidesLinkText: null,
    searchPlaceholder: "Search a city, country, or guide",
    showHero: true,
    showHeroStats: true,
    showGuideBrowser: true,
    showGuideBrowserControls: true,
    showMap: true,
    showCountrySections: true,
    searchLabel: "Search",
    showingLabel: "Showing",
    nearMeLabel: "Near me",
    countriesLabel: "Countries",
    allCountriesLabel: "All",
    emptyStateText: "No matching guides. Try a broader search or choose all countries.",
    searchResultsEyebrow: "Search results",
    searchResultsHeading: "Matching places",
    searchResultsGroupedLabel: "By guide",
    searchResultsIndividualLabel: "Individual",
    searchResultsEmptyText: "No matching places. Try a broader search.",
    guideCard: {
      showDescription: true,
      fallbackDescription: "Saved places for this guide.",
      showAuthor: true,
      authorLabel: "By",
      showTags: true,
      maxTags: 4,
      showPlaceCount: true,
      trimCountryFromTitle: true,
    },
  },
  guide: {
    fallbackDescription: "Saved places for this city.",
    showAuthor: true,
    authorLabel: "By",
    sidebarContent: [],
    backLinkLabel: "Back to all guides",
    sourceListLabel: "View source list",
    topPicksEyebrow: "Favorites",
    topPicksHeading: "Top suggestions",
    bestHitsEyebrow: "Crowd signal",
    bestHitsHeading: "Best hits",
    browseEyebrow: "Browse",
    placesHeading: "Places",
    showTopPicks: true,
    showBestHits: true,
    showGuideTags: true,
    listTagsHeading: "List tags",
    searchLabel: "Search",
    searchHint: "Type `#` to add a tag filter.",
    searchPlaceholder: "Search places, notes, neighborhoods, or type #tag",
    areaLabel: "Area",
    broaderAreaLabel: "Ward / district",
    typeLabel: "Type",
    popularTagsLabel: "Popular tags",
    popularTagsHint: "Click to add, or type `#` to browse all tags.",
    allFilterLabel: "All",
    resultsSortLabel: "Sort",
    sortCuratedLabel: "Curated",
    sortNearMeLabel: "Near me",
    sortRatingLabel: "Top rated",
    sortNameLabel: "Name",
    sortNeighborhoodLabel: "Area",
    locationSortFallbackText: "Location unavailable. Showing curated order instead.",
    resetMapLabel: "Reset map",
    mapFilteredText: "Filtered to the visible map area.",
    emptyStateText: "No matches. Try a broader search or clear the tag filter.",
  },
  placeCard: {
    showPhoto: true,
    showMapLink: true,
    showAttribution: true,
    showGuideAuthorAttribution: false,
    showCategory: true,
    showNeighborhood: true,
    showRating: true,
    showReviewCount: true,
    showPriceRange: true,
    showTrustSignals: true,
    showTopPickBadge: true,
    showWhyRecommended: true,
    showAddress: true,
    showTags: true,
    maxTags: 4,
    showStatus: true,
    bestHitLabel: "Popular",
    topPickLabel: "Top pick",
    attributionLabel: "Added by",
    mapsLabel: "Maps",
    savedPlaceLabel: "Saved place",
    photoPlaceholderFallback: "Saved favorite",
  },
};

function mergeSiteConfig(config: SiteConfigInput): SiteConfig {
  const { guideCard: legacyGuideCard, ...rootConfig } = config;

  return {
    ...defaultSiteConfig,
    ...rootConfig,
    favicon: config.favicon === undefined ? defaultSiteConfig.favicon : config.favicon,
    logo: config.logo === undefined ? defaultSiteConfig.logo : config.logo,
    navLinks: config.navLinks ?? defaultSiteConfig.navLinks,
    home: {
      ...defaultSiteConfig.home,
      ...config.home,
      guideCard: {
        ...defaultSiteConfig.home.guideCard,
        ...legacyGuideCard,
        ...config.home?.guideCard,
      },
    },
    guide: {
      ...defaultSiteConfig.guide,
      ...config.guide,
    },
    placeCard: {
      ...defaultSiteConfig.placeCard,
      ...config.placeCard,
    },
  };
}

export const siteConfig = mergeSiteConfig(configuredSiteConfig);
