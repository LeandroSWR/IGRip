/** Helpers for one downloaded post: slides, cover image, and server URLs. */

export function slidePaths(item) {
  const listed = Array.isArray(item?.saved_files) ? item.saved_files.filter(Boolean) : [];
  if (listed.length) return listed;
  return item?.saved_path ? [item.saved_path] : [];
}

export function isImagePath(path) {
  return /\.(jpe?g|png|webp|gif)$/i.test(path || "");
}

export function isCarousel(item) {
  return item?.media_type === "carousel" || slidePaths(item).length > 1;
}

/** First photo in a carousel, otherwise the first slide. */
export function coverPath(item) {
  const slides = slidePaths(item);
  return slides.find(isImagePath) || slides[0] || "";
}

export function mediaUrl(path) {
  if (!path) return "";
  return `/api/media?path=${encodeURIComponent(path)}`;
}

/** Grid tiles use a photo, or a cached still frame for videos. */
export function tileUrl(path) {
  if (!path) return "";
  if (isImagePath(path)) return mediaUrl(path);
  return `/api/poster?path=${encodeURIComponent(path)}`;
}
