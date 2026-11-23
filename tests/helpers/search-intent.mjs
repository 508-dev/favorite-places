// Complete model response fixture; individual tests override the slots they exercise.
export function searchIntent(fields) {
  return {
    turn_type: "new_search",
    alternatives: [],
    location: "",
    location_alternatives: [],
    preferences: [],
    min_rating: 0,
    ...fields,
  };
}
