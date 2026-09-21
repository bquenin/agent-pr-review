const REVIEW_ICON_SVG = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
  <path d="M12 2L14.09 8.26L20 9.27L15.55 13.97L16.91 20L12 16.9L7.09 20L8.45 13.97L4 9.27L9.91 8.26L12 2Z"/>
</svg>`;

const CHEVRON_ICON_SVG = `<svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <path d="M4.22 6.97a.75.75 0 0 1 1.06 0L8 9.69l2.72-2.72a.75.75 0 1 1 1.06 1.06L8.53 11.28a.75.75 0 0 1-1.06 0L4.22 8.03a.75.75 0 0 1 0-1.06Z"/>
</svg>`;

const REVIEW_URL_SCHEME = "agent-pr-review";
// Existing selections are retained; new installs use the configured default.
const REVIEW_CLI_STORAGE_KEY = "review-target";
const LEGACY_REVIEW_CLI_STORAGE_KEY = "review-cli";
const REVIEW_CLIS = {
  t3code: {
    menuLabel: "T3 Code",
    buttonLabel: "Review in T3 Code",
    title: "Start or resume this PR review in T3 Code on your dev host",
  },
  agent: {
    menuLabel: "Cursor (agent)",
    buttonLabel: "Review with Cursor",
    title: "Open a local Cursor agent review for this PR",
  },
  claude: {
    menuLabel: "Claude",
    buttonLabel: "Review with Claude Code",
    title: "Open a local Claude Code review for this PR",
  },
};

const launcherRenderers = new WeakMap();
let selectedCli = DEFAULT_REVIEW_CLI;
let selectionLoaded = false;
let statusPr = null;
let reviewStatus = "checking";
let statusSequence = 0;
let pendingStatus = null;
let lastStatusCheck = 0;
const T3_STATUS = {
  checking: ["Checking T3…", "Checking for an existing review. Click to launch in the background."],
  missing: ["Review in T3 Code", "Start this PR review in T3 on your dev host, without switching windows."],
  exists: ["Review exists in T3", "This PR already has a T3 review. Open T3 to read it; clicking here keeps its watcher running."],
  running: ["Reviewing in T3", "This PR review is running in T3. Clicking here will not send another prompt."],
  archived: ["Review archived in T3", "This PR has an archived T3 review. Click to restore it in the background."],
  error: ["T3 review needs attention", "A T3 review exists but failed to start or reported an error. Open T3 for details; click to retry."],
  unavailable: ["T3 status unavailable", "Could not check dev host/T3. Check the native status bridge and SSH connection. You can still click to launch the review."],
};

function refreshReviewStatus(force = false) {
  const pr = getPrUrl();
  if (pr !== statusPr) {
    statusPr = pr;
    reviewStatus = "checking";
    pendingStatus = null;
    lastStatusCheck = 0;
    statusSequence++;
    rerenderLaunchers();
  }
  if (!pr || !selectionLoaded || selectedCli !== "t3code" || document.hidden ||
      pendingStatus === pr || (!force && Date.now() - lastStatusCheck < 30000)) return;
  const sequence = ++statusSequence;
  pendingStatus = pr;
  lastStatusCheck = Date.now();
  let finished = false;
  const complete = (response) => {
    if (finished) return;
    finished = true;
    clearTimeout(timeout);
    if (sequence !== statusSequence || getPrUrl() !== pr) return;
    pendingStatus = null;
    const next = Object.hasOwn(T3_STATUS, response?.state) ? response.state : "unavailable";
    if (next !== reviewStatus) {
      reviewStatus = next;
      rerenderLaunchers();
    }
  };
  const timeout = setTimeout(() => complete(null), 25000);
  try {
    chrome.runtime.sendMessage({ type: "review-status", prUrl: `https://${pr}` }, (response) => {
      complete(chrome.runtime.lastError ? null : response);
    });
  } catch { complete(null); }
}

function normalizeCli(cli) {
  return REVIEW_CLIS[cli] ? cli : DEFAULT_REVIEW_CLI;
}

function getStorageArea() {
  return chrome?.storage?.local ?? null;
}

function getLegacySelectedCli() {
  try {
    return normalizeCli(window.localStorage.getItem(REVIEW_CLI_STORAGE_KEY));
  } catch {
    return DEFAULT_REVIEW_CLI;
  }
}

function clearLegacySelectedCli(storage = null) {
  try {
    window.localStorage.removeItem(REVIEW_CLI_STORAGE_KEY);
    window.localStorage.removeItem(LEGACY_REVIEW_CLI_STORAGE_KEY);
  } catch {
    // Ignore storage cleanup failures in the page context.
  }
  storage?.remove?.([LEGACY_REVIEW_CLI_STORAGE_KEY]);
}

function loadSelectedCli() {
  const storage = getStorageArea();
  if (!storage) {
    return Promise.resolve(getLegacySelectedCli());
  }

  return new Promise((resolve) => {
    storage.get([REVIEW_CLI_STORAGE_KEY, LEGACY_REVIEW_CLI_STORAGE_KEY], (items) => {
      if (chrome.runtime?.lastError) {
        resolve(getLegacySelectedCli());
        return;
      }

      if (Object.prototype.hasOwnProperty.call(items, REVIEW_CLI_STORAGE_KEY)) {
        clearLegacySelectedCli(storage);
        resolve(normalizeCli(items[REVIEW_CLI_STORAGE_KEY]));
        return;
      }

      const migratedCli = items[LEGACY_REVIEW_CLI_STORAGE_KEY]
        ? normalizeCli(items[LEGACY_REVIEW_CLI_STORAGE_KEY]) : getLegacySelectedCli();
      storage.set({ [REVIEW_CLI_STORAGE_KEY]: migratedCli }, () => {
        if (!chrome.runtime?.lastError) {
          clearLegacySelectedCli(storage);
        }
        resolve(migratedCli);
      });
    });
  });
}

function persistSelectedCli(cli) {
  selectedCli = normalizeCli(cli);

  const storage = getStorageArea();
  if (storage) {
    storage.set({ [REVIEW_CLI_STORAGE_KEY]: selectedCli }, () => {
      if (!chrome.runtime?.lastError) {
        clearLegacySelectedCli(storage);
      }
    });
    return;
  }

  try {
    window.localStorage.setItem(REVIEW_CLI_STORAGE_KEY, selectedCli);
  } catch {
    // Ignore storage write failures in the page context.
  }
}

function getPrUrl() {
  const match = window.location.pathname.match(
    /^\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)\/pull\/([1-9][0-9]*)(?:\/|$)/
  );
  if (!match) return null;
  // Normalize: always return host/owner/repo/pull/number
  const [, owner, repo, number] = match;
  if ([owner, repo].some(part => part === "." || part === "..")) return null;
  return `${window.location.host.toLowerCase()}/${owner.toLowerCase()}/${repo.toLowerCase()}/pull/${number}`;
}

function getLaunchUrl(cli, prPath) {
  const params = new URLSearchParams({ cli: normalizeCli(cli) });
  return `${REVIEW_URL_SCHEME}://${prPath}?${params.toString()}`;
}

function findInsertionPoint() {
  // Strategy: try multiple selectors, broadest to most specific
  const selectors = [
    // GitHub.com and GHE: action buttons container in PR header
    ".gh-header-actions",
    // GHE / older GitHub: the flex row-reverse container with Edit button
    "#partial-discussion-header .flex-md-row-reverse",
    // Another common pattern: header actions area
    ".gh-header-show .flex-md-row-reverse",
    // Fallback: find the Edit button and use its parent
    null, // handled separately below
  ];

  for (const sel of selectors) {
    if (sel) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
  }

  // Last resort: find any element that contains the "Edit" button near the PR title
  const editBtn = Array.from(document.querySelectorAll("button")).find(
    (b) =>
      b.textContent.trim() === "Edit" &&
      b.closest(
        "#partial-discussion-header, .gh-header-show, .js-issue-header-edit-button"
      )
  );
  if (editBtn) return editBtn.parentElement;

  return null;
}

function setMenuOpen(launcher, isOpen) {
  launcher.classList.toggle("is-open", isOpen);
  const toggle = launcher.querySelector(".review-launcher-toggle");
  if (toggle) {
    toggle.setAttribute("aria-expanded", String(isOpen));
  }
}

function closeOpenMenus(exceptLauncher = null) {
  document.querySelectorAll(".review-launcher.is-open").forEach((launcher) => {
    if (launcher !== exceptLauncher) {
      setMenuOpen(launcher, false);
    }
  });
}

function renderLauncher(launcher) {
  const cli = normalizeCli(selectedCli);
  const meta = cli === "t3code"
    ? { buttonLabel: T3_STATUS[reviewStatus][0], title: T3_STATUS[reviewStatus][1] }
    : REVIEW_CLIS[cli];
  const primaryButton = launcher.querySelector(".review-launcher-primary");
  const menu = launcher.querySelector(".review-launcher-menu");

  primaryButton.innerHTML = `${REVIEW_ICON_SVG} ${meta.buttonLabel}`;
  primaryButton.title = meta.title;
  launcher.classList.toggle("has-t3-review", cli === "t3code" && ["exists", "running", "archived"].includes(reviewStatus));
  launcher.classList.toggle("t3-status-unavailable", cli === "t3code" && reviewStatus === "unavailable");

  menu.querySelectorAll(".review-launcher-option").forEach((option) => {
    const optionCli = option.dataset.cli;
    option.classList.toggle("is-selected", optionCli === cli);
    option.setAttribute("aria-checked", String(optionCli === cli));
  });
}

function createLauncher() {
  const launcher = document.createElement("div");
  launcher.className = "review-launcher";

  const primaryButton = document.createElement("button");
  primaryButton.type = "button";
  primaryButton.className = "review-launcher-primary";

  const toggleButton = document.createElement("button");
  toggleButton.type = "button";
  toggleButton.className = "review-launcher-toggle";
  toggleButton.innerHTML = CHEVRON_ICON_SVG;
  toggleButton.title = "Choose review tool";
  toggleButton.setAttribute("aria-haspopup", "menu");
  toggleButton.setAttribute("aria-expanded", "false");

  const menu = document.createElement("div");
  menu.className = "review-launcher-menu";
  menu.setAttribute("role", "menu");

  for (const [cli, meta] of Object.entries(REVIEW_CLIS)) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "review-launcher-option";
    option.dataset.cli = cli;
    option.setAttribute("role", "menuitemradio");
    option.textContent = meta.menuLabel;
    option.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      persistSelectedCli(cli);
      rerenderLaunchers();
      refreshReviewStatus(true);
      setMenuOpen(launcher, false);
    });
    menu.appendChild(option);
  }

  primaryButton.addEventListener("click", (event) => {
    event.preventDefault();
    const prPath = getPrUrl();
    if (!prPath) return;
    window.location.href = getLaunchUrl(selectedCli, prPath);
    if (selectedCli === "t3code") {
      // The URL handoff has no acknowledgement; confirm the thread via live status.
      [2000, 5000, 10000, 20000].forEach((delay) => setTimeout(() => {
        if (getPrUrl() === prPath) refreshReviewStatus(true);
      }, delay));
    }
  });

  toggleButton.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const willOpen = !launcher.classList.contains("is-open");
    closeOpenMenus(launcher);
    setMenuOpen(launcher, willOpen);
  });

  launcher.append(primaryButton, toggleButton, menu);
  launcherRenderers.set(launcher, () => renderLauncher(launcher));
  renderLauncher(launcher);
  return launcher;
}

function rerenderLaunchers() {
  document.querySelectorAll(".review-launcher").forEach((launcher) => {
    const render = launcherRenderers.get(launcher);
    if (render) render();
  });
}

function injectLauncher() {
  if (document.querySelector(".review-launcher")) return;
  if (!getPrUrl()) return;

  const target = findInsertionPoint();
  if (!target) return;

  target.prepend(createLauncher());
}

function ensureSelectionThenInject() {
  if (selectionLoaded) {
    injectLauncher();
    refreshReviewStatus();
    return;
  }

  loadSelectedCli().then((cli) => {
    selectedCli = normalizeCli(cli);
    selectionLoaded = true;
    injectLauncher();
    rerenderLaunchers();
    refreshReviewStatus();
  });
}

document.addEventListener("click", () => closeOpenMenus());
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeOpenMenus();
});

// Re-inject on GitHub SPA navigation (turbo/pjax)
const observer = new MutationObserver(() => {
  if (!document.querySelector(".review-launcher") && getPrUrl()) {
    ensureSelectionThenInject();
  }
  if (getPrUrl() !== statusPr) refreshReviewStatus();
});

observer.observe(document.body, { childList: true, subtree: true });

// Also handle popstate for back/forward navigation
window.addEventListener("popstate", () =>
  setTimeout(ensureSelectionThenInject, 100)
);

ensureSelectionThenInject();
window.addEventListener("focus", () => refreshReviewStatus(true));
document.addEventListener("visibilitychange", () => refreshReviewStatus(true));
setInterval(() => refreshReviewStatus(), 30000);
