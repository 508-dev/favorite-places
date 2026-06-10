export type FieldSource =
  | "manual"
  | "google_list"
  | "google_maps_page"
  | "google_places"
  | "osm"
  | "wikidata"
  | "website"
  | "trust_signal";
export type TrustSignalSource =
  | "michelin"
  | "tabelog"
  | "timeout"
  | "blog"
  | "web"
  | "brave_search"
  | "google_search";
export type TrustSignalConfidence = "high" | "medium" | "low";
export type MarkerIcon =
  | "default"
  | "cafe"
  | "restaurant"
  | "bar"
  | "bakery"
  | "museum"
  | "attraction"
  | "park"
  | "beach"
  | "shopping"
  | "hotel"
  | "spa";

export interface PlaceField<T> {
  value: T;
  source: FieldSource;
  fetched_at?: string | null;
  expires_at?: string | null;
}

export interface PlaceProvenance {
  name?: PlaceField<string> | null;
  address?: PlaceField<string> | null;
  lat?: PlaceField<number> | null;
  lng?: PlaceField<number> | null;
  maps_url?: PlaceField<string> | null;
  cid?: PlaceField<string> | null;
  google_id?: PlaceField<string> | null;
  google_place_id?: PlaceField<string> | null;
  google_place_resource_name?: PlaceField<string> | null;
  rating?: PlaceField<number> | null;
  user_rating_count?: PlaceField<number> | null;
  primary_category?: PlaceField<string> | null;
  primary_category_localized?: PlaceField<string> | null;
  tags: PlaceField<string>[];
  locality_path?: PlaceField<string[]> | null;
  neighborhood?: PlaceField<string> | null;
  note?: PlaceField<string> | null;
  why_recommended?: PlaceField<string> | null;
  added_by?: PlaceField<ListAuthor> | null;
  top_pick?: PlaceField<boolean> | null;
  hidden?: PlaceField<boolean> | null;
  manual_rank?: PlaceField<number> | null;
  status?: PlaceField<string> | null;
  trust_signals?: PlaceField<TrustSignal[]> | null;
}

export interface TrustSignal {
  source: TrustSignalSource;
  label: string;
  tier?: string | null;
  award_year?: number | null;
  is_current?: boolean | null;
  url?: string | null;
  title?: string | null;
  published_at?: string | null;
  fetched_at: string;
  confidence: TrustSignalConfidence;
  match_reason: string;
}

export interface Place {
  id: string;
  name: string;
  address: string | null;
  lat: number | null;
  lng: number | null;
  maps_url: string;
  cid: string | null;
  google_id: string | null;
  google_place_id: string | null;
  google_place_resource_name: string | null;
  rating: number | null;
  user_rating_count: number | null;
  primary_category: string | null;
  primary_category_localized?: string | null;
  marker_icon: MarkerIcon;
  tags: string[];
  visible_tags: string[];
  vibe_tags: string[];
  price_range: string | null;
  locality_path: string[];
  neighborhood: string | null;
  note: string | null;
  why_recommended: string | null;
  added_by?: ListAuthor | null;
  main_photo_path: string | null;
  photo_url: string | null;
  top_pick: boolean;
  hidden: boolean;
  manual_rank: number;
  status: string;
  trust_signals?: TrustSignal[];
  provenance: PlaceProvenance;
}

export interface ListAuthor {
  name: string;
  photo_url?: string | null;
  photo_path?: string | null;
  avatar_mode?: "photo" | "initials" | "icon" | null;
  profile_id?: string | null;
}

export interface Guide {
  slug: string;
  title: string;
  description: string | null;
  author?: ListAuthor | null;
  place_photo_mode: "local_cache" | "remote_url";
  source_url: string | null;
  list_id: string | null;
  country_name: string;
  country_code: string | null;
  city_name: string;
  list_tags: string[];
  featured_place_ids: string[];
  best_hit_place_ids: string[];
  best_hit_min_rating: number | null;
  best_hit_min_reviews: number | null;
  top_categories: string[];
  generated_at: string;
  place_count: number;
  center_lat: number | null;
  center_lng: number | null;
  places: Place[];
}

export interface GuideManifest {
  slug: string;
  title: string;
  description: string | null;
  author?: ListAuthor | null;
  country_name: string;
  country_code: string | null;
  center_lat: number | null;
  center_lng: number | null;
  city_name: string;
  list_tags: string[];
  place_count: number;
  featured_names: string[];
  top_categories: string[];
}
