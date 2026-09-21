const STATUS_HOST = "com.agent_pr_review.status";
const STATUS_STATES = new Set(["missing", "exists", "running", "archived", "error", "unavailable"]);
const ALLOWED_HOSTS = new Set(chrome.runtime.getManifest().host_permissions.map(pattern => new URL(pattern).hostname));
const statusCache = new Map();
const statusRequests = new Map();

function canonicalPr(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.port ||
        !ALLOWED_HOSTS.has(parsed.hostname)) return null;
    const match = parsed.pathname.match(/^\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)\/pull\/([1-9][0-9]*)(?:\/|$)/);
    return match && ![match[1], match[2]].some(part => part === "." || part === "..")
      ? `${parsed.origin}/${match[1].toLowerCase()}/${match[2].toLowerCase()}/pull/${match[3]}` : null;
  } catch { return null; }
}

function readReviewStatus(prUrl) {
  const cached = statusCache.get(prUrl);
  if (cached && Date.now() - cached.at < 3000) return Promise.resolve(cached.result);
  if (statusRequests.has(prUrl)) return statusRequests.get(prUrl);
  if (statusRequests.size >= 4) return Promise.resolve({ state: "unavailable" });
  const request = new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(STATUS_HOST, { type: "review-status", prUrl }, (response) => {
      const result = !chrome.runtime.lastError && STATUS_STATES.has(response?.state)
        ? { state: response.state } : { state: "unavailable" };
      if (statusCache.size >= 64) statusCache.delete(statusCache.keys().next().value);
      statusCache.set(prUrl, { result, at: Date.now() });
      resolve(result);
    });
  }).catch(() => ({ state: "unavailable" })).finally(() => statusRequests.delete(prUrl));
  statusRequests.set(prUrl, request);
  return request;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Never let a content script query another page's PR or arbitrary SSH commands.
  const prUrl = canonicalPr(sender.url);
  if (sender.id !== chrome.runtime.id || !sender.tab || !prUrl ||
      message?.type !== "review-status" || message.prUrl !== prUrl) {
    sendResponse({ state: "unavailable" });
    return false;
  }
  readReviewStatus(prUrl).then(sendResponse);
  return true;
});
