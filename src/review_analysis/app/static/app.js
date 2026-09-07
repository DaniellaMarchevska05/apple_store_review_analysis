"use strict";

const $ = (id) => document.getElementById(id);
let collection = null;
let busy = false;
let storefronts = {};

let countryCode = "us";
let countryMatches = [];
let activeCountry = -1;

function closeCountries() {
  $("country-options").hidden = true;
  $("country").setAttribute("aria-expanded", "false");
  $("country").removeAttribute("aria-activedescendant");
  $("country").value = storefronts[countryCode] || "United States";
}

function chooseCountry(code) {
  countryCode = code;
  closeCountries();
  $("search-results").replaceChildren();
}

function highlightCountry(index) {
  activeCountry = index;
  const options = $("country-options").querySelectorAll('[role="option"]');
  options.forEach((option, i) => option.classList.toggle("active", i === index));
  const option = options[index];
  if (option) {
    $("country").setAttribute("aria-activedescendant", option.id);
    option.scrollIntoView({block: "nearest"});
  } else {
    $("country").removeAttribute("aria-activedescendant");
  }
}

function openCountries(query = "") {
  if (busy || !Object.keys(storefronts).length) return;
  const normalize = (value) => value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const term = normalize(query.trim());
  countryMatches = Object.entries(storefronts).filter(([code, name]) =>
    normalize(name).includes(term) || code.includes(term));
  $("country-options").replaceChildren(...countryMatches.map(([code, name]) => {
    const option = element("div", "country-option");
    option.id = `country-${code}`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", String(code === countryCode));
    option.append(element("span", "", name), element("span", "country-code", code.toUpperCase()));
    option.addEventListener("mousedown", (event) => event.preventDefault());
    option.addEventListener("click", () => chooseCountry(code));
    return option;
  }));
  if (!countryMatches.length) {
    $("country-options").append(element("p", "country-empty", "No countries found."));
  }
  $("country-options").hidden = false;
  $("country").setAttribute("aria-expanded", "true");
  $("country-options").scrollTop = 0;
  highlightCountry(-1);
}

async function loadStorefronts() {
  storefronts = await request("/storefronts");
  $("country").disabled = busy;
}

$("country").addEventListener("focus", () => {
  $("country").select();
  openCountries();
});
$("country").addEventListener("click", () => {
  if ($("country-options").hidden) { $("country").select(); openCountries(); }
});
$("country").addEventListener("input", () => openCountries($("country").value));
$("country").addEventListener("blur", () => {
  const typed = $("country").value.trim().toLowerCase();
  const match = Object.entries(storefronts).find(([code, name]) => code === typed || name.toLowerCase() === typed);
  if (match) chooseCountry(match[0]);
  else closeCountries();
});
$("country").addEventListener("keydown", (event) => {
  if (event.key === "Escape") { event.preventDefault(); closeCountries(); return; }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if ($("country-options").hidden) openCountries();
    const direction = event.key === "ArrowDown" ? 1 : -1;
    if (countryMatches.length) {
      highlightCountry((activeCountry + direction + countryMatches.length) % countryMatches.length);
    }
  } else if (event.key === "Enter" && !$("country-options").hidden) {
    event.preventDefault();
    const index = activeCountry >= 0 ? activeCountry : 0;
    if (countryMatches[index]) chooseCountry(countryMatches[index][0]);
  }
});

// External app names and review text are always inserted as text, never HTML.
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function request(path, options = {}) {
  // Server operation deadlines are configurable up to 600 seconds; allow a little overhead.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.method === "POST" ? 615000 : 30000);
  try {
    const response = await fetch(`/api/v1${path}`, {...options, signal: controller.signal});
    let body;
    try {
      body = await response.json();
    } catch {
      throw new Error("The service returned an unreadable response. Please try again.");
    }
    if (!response.ok) {
      const error = new Error(body?.error?.message || "Something went wrong. Please try again.");
      error.code = body?.error?.code;
      throw error;
    }
    return body;
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error("The request timed out. Reopen your saved snapshot before retrying; work may still finish.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function setBusy(value, message = "") {
  busy = value;
  if (value) closeCountries();
  document.querySelectorAll("button, #query, #country").forEach((node) => {
    node.disabled = value || (node.id === "country" && !Object.keys(storefronts).length);
  });
  $("status").hidden = !message;
  $("status").textContent = message;
  $("status").classList.toggle("busy", value);
}

function showError(error) {
  $("error").textContent = error instanceof TypeError ?
    "Could not reach the service. Check your connection and try again." :
    error.message;
  $("error").hidden = false;
}

function clearError() {
  $("error").hidden = true;
}

function collectionPath() {
  return `/collections/${encodeURIComponent(collection.id)}`;
}

function bars(target, entries) {
  target.replaceChildren();
  for (const [label, bucket, color] of entries) {
    const row = element("div", "bar-row");
    const track = element("div", "track");
    const fill = element("div", "fill");
    fill.style.width = `${Math.max(0, Math.min(100, bucket.percentage))}%`;
    if (color) fill.style.background = color;
    track.append(fill);
    row.append(element("span", "", label), track,
      element("span", "bar-value", `${bucket.count} · ${bucket.percentage}%`));
    target.append(row);
  }
}

function showCollection(metrics) {
  $("report").hidden = false;
  $("empty-state").hidden = true;
  $("report-title").textContent = collection.app_name;
  const date = collection.collected_at ?
    ` · ${new Date(collection.collected_at).toLocaleDateString()}` : "";
  $("report-meta").textContent = `${collection.country.toUpperCase()} storefront${date}`;
  $("average").textContent = metrics.average_rating === null ?
    "—" : metrics.average_rating.toFixed(2);
  $("sample-count").textContent = metrics.review_count;
  $("csv-link").href = `/api/v1${collectionPath()}/reviews?format=csv`;
  $("json-link").href = `/api/v1${collectionPath()}/reviews?format=json`;
  bars($("ratings"), [5, 4, 3, 2, 1].map((star) => [`${star} star`, metrics.distribution[star]]));
  showWarnings([]);
  $("analysis-action").hidden = metrics.review_count === 0;
}

function showWarnings(warnings) {
  $("warnings").replaceChildren(...[...new Set([
    ...collection.warnings, ...warnings,
    "This recent-review sample is not the app’s lifetime rating or a population estimate.",
  ])].map((text) => element("li", "", text)));
}

function reviewExcerpts(items) {
  const evidence = element("details", "evidence");
  evidence.append(element("summary", "", "Read supporting reviews"));
  items.forEach((item) => {
    const quote = element("blockquote", "", item.excerpt);
    quote.dir = "auto";
    quote.append(element("footer", "", `Review ${item.review_id}`));
    evidence.append(quote);
  });
  return evidence;
}

function showStrengths(strengths) {
  $("strengths").replaceChildren();
  if (strengths === null || strengths === undefined) {
    $("strengths").append(element("p", "panel", "This saved analysis does not include strengths."));
    return;
  }
  if (!strengths.length) {
    $("strengths").append(element("p", "panel", "No specific strengths were supported by this sample."));
  }
  strengths.forEach((strength, index) => {
    const card = element("article", "insight strength");
    const content = element("div");
    content.append(element("h3", "", strength.theme),
      element("span", "support", `${strength.supporting_review_count} supporting reviews`),
      element("p", "problem", strength.summary));
    if (strength.evidence.length) content.append(reviewExcerpts(strength.evidence));
    card.append(element("span", "insight-number", String(index + 1).padStart(2, "0")), content);
    $("strengths").append(card);
  });
}

function showAnalysis(analysis) {
  $("analysis-action").hidden = true;
  $("findings").hidden = false;
  const sentiment = analysis.sentiment;
  const historical = analysis.language_scope === "english";
  $("sentiment-scope").textContent = historical ? "English-only saved analysis" : "Interpretable reviews";
  $("negative").textContent = sentiment.analyzed_count ?
    `${sentiment.distribution.negative.percentage}%` : "—";
  $("coverage").textContent =
    `${sentiment.analyzed_count} analyzed · ${sentiment.unsupported_count} uninterpretable`;
  bars($("sentiment"), [
    ["Positive", sentiment.distribution.positive, "#6e8d69"],
    ["Negative", sentiment.distribution.negative, "#b36851"],
    ["Neutral", sentiment.distribution.neutral, "#a5ac99"],
  ]);
  showStrengths(analysis.strengths);
  $("insights").replaceChildren();
  analysis.insights.forEach((insight, index) => {
    const card = element("article", "insight");
    const content = element("div");
    content.append(element("h3", "", insight.theme),
      element("span", "support", `${insight.supporting_review_count} supporting reviews`));
    if (insight.problem_summary) content.append(element("p", "problem", insight
      .problem_summary));
    const actions = element("div", "next-step");
    actions.append(element("h4", "", "Recommended action"), element("p", "", insight
      .recommendation));
    content.append(actions);
    if (insight.examples.length) content.append(reviewExcerpts(insight.examples));
    card.append(element("span", "insight-number", String(index + 1).padStart(2, "0")), content);
    $("insights").append(card);
  });
  if (!analysis.insights.length) {
    $("insights").append(element("p", "panel",
      "No actionable improvement themes were identified in this sample."));
  }
  $("phrases").replaceChildren();
  analysis.negative_phrases.forEach((phrase) => {
    const chip = element("div", "phrase", phrase.text);
    chip.dir = "auto";
    chip.append(element("span", "", phrase.review_count));
    $("phrases").append(chip);
  });
  if (!analysis.negative_phrases.length) $("phrases").textContent = "No recurring phrases found.";
  showWarnings(analysis.warnings);
}

async function analyze() {
  setBusy(true, "Reading reviews and preparing findings. This may take several minutes…");
  $("analysis-action").hidden = true;
  try {
    showAnalysis(await request(`${collectionPath()}/analysis`, {
      method: "POST"
    }));
    setBusy(false, "Your review snapshot is ready.");
  } catch (error) {
    setBusy(false);
    $("analysis-action").hidden = false;
    $("analyze-button").textContent = "Retry analysis ↗";
    showError(error);
    $("analysis-action").querySelector("p").textContent = $("error").textContent;
  }
}

async function selectApp(app, country) {
  if (busy) return;
  clearError();
  collection = null;
  $("report").hidden = true;
  $("empty-state").hidden = true;
  $("findings").hidden = true;
  $("negative").textContent = "—";
  $("coverage").textContent = "Analysis pending";
  $("sentiment-scope").textContent = "Interpretable reviews";
  $("analysis-action").querySelector("p").textContent = "Ratings and downloads are ready. Continue to see the review findings.";
  $("sentiment").replaceChildren(element("p", "muted", "Preparing sentiment analysis…"));
  $("analyze-button").textContent = "Analyze reviews ↗";
  history.replaceState(null, "", location.pathname);
  setBusy(true, `Collecting recent reviews for ${app.app_name}…`);
  try {
    collection = await request("/collections", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        app_id: app.app_id,
        country
      }),
    });
    history.replaceState(null, "", `?collection=${encodeURIComponent(collection.id)}`);
    const metrics = await request(`${collectionPath()}/metrics`);
    showCollection(metrics);
    $("report-title").focus({
      preventScroll: true
    });
    $("report").scrollIntoView({
      behavior: "instant",
      block: "start"
    });
    if (metrics.review_count) await analyze();
    else setBusy(false, "No reviews were available in this storefront. Try another storefront.");
  } catch (error) {
    setBusy(false);
    showError(error);
    $("empty-state").hidden = false;
  }
}

$("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  clearError();
  const query = $("query").value.trim();
  if (query.length < 2) {
    showError(new Error("Enter at least two characters to search."));
    return;
  }
  const country = countryCode;
  $("search-results").replaceChildren();
  setBusy(true, "Finding matching apps…");
  try {
    const apps = await request(
      `/apps/search?${new URLSearchParams({query, country, limit: "5"})}`);
    apps.forEach((app) => {
      const row = element("div", "app-result");
      const details = element("div");
      details.append(element("strong", "", app.app_name), element("p", "", app
      .developer));
      try {
        const url = new URL(app.app_url);
        if (url.protocol === "https:" && url.hostname === "apps.apple.com") {
          const link = element("a", "quiet-link", "View in App Store ↗");
          link.href = url.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          details.append(link);
        }
      } catch {
        /* The app can still be selected if its store link is unavailable. */ }
      const button = element("button", "", "Analyze app ↗");
      button.type = "button";
      button.setAttribute("aria-label", `Analyze ${app.app_name} by ${app.developer}`);
      button.addEventListener("click", () => selectApp(app, country));
      row.append(details, button);
      $("search-results").append(row);
    });
    if (!apps.length) $("search-results").append(element("p", "muted",
      "No matches found. Try another name or storefront."));
    setBusy(false, apps.length ? "Choose the app you want to analyze." : "");
  } catch (error) {
    setBusy(false);
    showError(error);
  }
});

$("analyze-button").addEventListener("click", () => {
  if (!busy && collection) {
    clearError();
    analyze();
  }
});

async function restoreCollection() {
  const id = new URLSearchParams(location.search).get("collection");
  if (!id) return;
  if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id)) {
    showError(new Error("This collection link is invalid. Search for an app to start again."));
    return;
  }
  setBusy(true, "Opening your saved snapshot…");
  try {
    collection = await request(`/collections/${id}`);
    showCollection(await request(`${collectionPath()}/metrics`));
    try {
      showAnalysis(await request(`${collectionPath()}/analysis`));
    } catch (error) {
      if (error.code !== "analysis_not_found") throw error;
    }
    setBusy(false);
  } catch (error) {
    setBusy(false);
    showError(error);
  }
}

// Saved snapshots remain accessible if the country catalog cannot be loaded.
loadStorefronts().catch(showError);
restoreCollection();
